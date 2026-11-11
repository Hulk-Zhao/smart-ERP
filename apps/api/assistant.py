"""助手建议：OpenAI 兼容 LLM + 规则降级。

只做一类建议：``next_action``（这张工单下一步怎么处理）。

边界：

- 发给模型的只有该工单自身、且经白名单裁剪的字段（``ticket_payload``）。
  不发 ``tenant_id`` / ``initiator_id`` / ``business_key`` / ``object_id`` /
  ``idem_key`` / 审计影像，也不发会话令牌。
- 密钥只从配置读取，绝不写日志；日志只有主机名、模型名、耗时与结果。
- LLM 不可用时一律降级为规则建议（``source="rule"`` + ``fallback_reason``），
  不向页面抛 5xx。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx

from apps.api.store import TicketRecord
from apps.platform.api.errors import ProblemError
from apps.platform.config import Settings, get_settings
from apps.platform.contracts.errors import ErrorCode
from apps.platform.policy.levels import WriteLevel

logger = logging.getLogger(__name__)

#: 助手建议的硬超时：与通用 ``ERP_LLM_TIMEOUT_SECONDS`` 解耦，避免交互页面等 60s。
LLM_TIMEOUT_SECONDS = 8.0
RULE_MODEL = "rule/next_action-v1"
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

_SYSTEM_PROMPT = (
    "你是 ERP 审单助手。根据给定工单的当前状态、写等级、金额与已执行步骤，"
    "给出一句可执行的中文下一步建议，并列出不超过 3 条依据。"
    "只输出 JSON：{\"suggestion\": \"...\", \"reasons\": [\"...\"]}。"
    "不要输出 JSON 以外的任何内容。"
)


class RateLimiter:
    """单实例内存令牌桶（按 key 分桶），用于每租户每分钟限流。"""

    def __init__(
        self, per_minute: int, clock: Callable[[], float] | None = None
    ) -> None:
        self.capacity = float(max(per_minute, 1))
        self.refill_per_second = self.capacity / 60.0
        self._clock = clock or time.monotonic
        self._state: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        tokens, last = self._state.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
        if tokens < 1.0:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1.0, now)
        return True


@dataclass(frozen=True, slots=True)
class Suggestion:
    suggestion: str
    reasons: tuple[str, ...]
    source: str
    model: str
    generated_at: datetime
    fallback_reason: str | None = None


@dataclass
class AssistantService:
    """LLM 客户端 + 规则降级；``client`` 可注入（测试用 MockTransport）。"""

    base_url: str = ""
    #: 密钥不参与 repr，避免异常日志把整段配置打出来。
    api_key: str = field(default="", repr=False)
    model: str = ""
    rate_limit_per_minute: int = 10
    client: httpx.Client | None = None
    clock: Callable[[], float] = time.monotonic
    _limiter: RateLimiter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._limiter = RateLimiter(self.rate_limit_per_minute, self.clock)
        # 环境装配不带 client；已配置时补一个，测试用 MockTransport 注入的优先。
        if self.client is None and self.configured:
            self.client = httpx.Client()

    @classmethod
    def from_env(cls) -> AssistantService:
        settings: Settings = get_settings()
        return cls(
            base_url=settings.llm_base_url.strip(),
            api_key=settings.llm_api_key.strip(),
            model=settings.llm_model.strip(),
            rate_limit_per_minute=settings.assistant_rate_limit_per_minute,
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def suggest(self, *, ticket: TicketRecord, tenant_id: UUID) -> Suggestion:
        """先限流，再尝试 LLM，任何失败都降级为规则建议。"""

        if not self._limiter.allow(str(tenant_id)):
            raise ProblemError(
                ErrorCode.CONN_RATE_LIMIT,
                "助手建议请求过于频繁（每租户每分钟上限），请稍后重试",
            )

        rule = _rule_suggestion(ticket)
        if not self.configured:
            return _with_fallback(rule, "llm_not_configured")

        started = time.monotonic()
        host = urlparse(self.base_url).hostname or "unknown"
        try:
            payload = self._call(ticket)
        except httpx.TimeoutException:
            self._log(host, "timeout", started)
            return _with_fallback(rule, "llm_timeout")
        except httpx.TransportError as exc:
            self._log(host, f"transport_{type(exc).__name__}", started)
            return _with_fallback(rule, "llm_connect_failed")
        except _HttpFailure as exc:
            self._log(host, f"http_{exc.status}", started)
            return _with_fallback(rule, f"llm_http_{exc.status}")
        except Exception as exc:  # noqa: BLE001 - 兜底不向页面抛 5xx
            self._log(host, f"error_{type(exc).__name__}", started)
            return _with_fallback(rule, "llm_error")

        parsed = _parse_completion(payload)
        if parsed is None:
            self._log(host, "invalid_output", started)
            return _with_fallback(rule, "llm_output_invalid")

        self._log(host, "ok", started)
        suggestion, reasons = parsed
        return Suggestion(
            suggestion=suggestion,
            reasons=reasons,
            source="llm",
            model=self.model,
            generated_at=datetime.now(UTC),
        )

    def _call(self, ticket: TicketRecord) -> dict[str, Any]:
        assert self.client is not None  # configured 已保证
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(ticket_payload(ticket), ensure_ascii=False),
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in (1, 2):
            response = self.client.post(
                url, headers=headers, json=body, timeout=LLM_TIMEOUT_SECONDS
            )
            if response.status_code in _RETRYABLE_STATUS and attempt == 1:
                continue
            if response.status_code >= 400:
                # 只记录状态码，不读取/记录响应体（可能回显请求内容）。
                raise _HttpFailure(response.status_code)
            return response.json()
        raise AssertionError("unreachable")  # pragma: no cover

    def _log(self, host: str, outcome: str, started: float) -> None:
        logger.info(
            "assistant.suggest host=%s model=%s elapsed_ms=%d outcome=%s",
            host,
            self.model or "-",
            int((time.monotonic() - started) * 1000),
            outcome,
        )


class _HttpFailure(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"llm http {status}")
        self.status = status


def _with_fallback(rule: Suggestion, reason: str) -> Suggestion:
    return Suggestion(
        suggestion=rule.suggestion,
        reasons=rule.reasons,
        source="rule",
        model=rule.model,
        generated_at=datetime.now(UTC),
        fallback_reason=reason,
    )


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _level_name(value: WriteLevel | None) -> str | None:
    return value.name if value is not None else None


def ticket_payload(ticket: TicketRecord) -> dict[str, Any]:
    """发给模型的白名单字段；新增字段必须在这里显式列出。"""

    return {
        "ticket_no": ticket.ticket_no,
        "scene_code": ticket.scene_code,
        "state": _enum_value(ticket.state),
        "write_level": _level_name(ticket.write_level),
        "amount": str(ticket.amount) if ticket.amount is not None else None,
        "touches_fund": ticket.touches_fund,
        "period": ticket.period,
        "systems": list(ticket.systems or ()),
        "version": ticket.version,
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "steps": [
            {
                "step_no": step.step_no,
                "action_key": step.action_key,
                "system": step.system,
                "object_type": step.object_type,
                "level": _level_name(step.level),
                "state": step.state,
                "executed_at": (
                    step.executed_at.isoformat() if step.executed_at else None
                ),
            }
            for step in ticket.steps
        ],
        "pending_write": (
            {
                "action_key": ticket.pending_write.action_key,
                "system": ticket.pending_write.system,
            }
            if ticket.pending_write is not None
            else None
        ),
    }


def _rule_suggestion(ticket: TicketRecord) -> Suggestion:
    state = _enum_value(ticket.state)
    level = _level_name(ticket.write_level)
    reasons: list[str] = [f"工单当前状态：{state}"]
    if level:
        reasons.append(f"写等级：{level}")
    if ticket.amount is not None:
        reasons.append(f"金额：{ticket.amount}")
    if ticket.touches_fund:
        reasons.append("涉及资金，审批与校验需留痕")

    actions = {
        "计划中": "先补齐执行前核对项，再提交审批。",
        "待确认": "确认场景要素与写计划后再继续。",
        "待审批": "按写等级推进审批；审批通过前不要执行外部写入。",
        "执行中": "跟进执行回执，拿到外部单号后进入校验。",
        "校验中": "核对校验结果，确认一致后收口。",
        "已完成": "归档证据并关闭工单。",
        "补偿中": "跟进补偿结果，确认回滚闭环。",
        "已回滚": "复核回滚原因，确认是否需要重开工单。",
        "需人工处置": "转人工处置，补齐缺失信息后再重试。",
        "已锁定": "先解除锁定，当前状态禁止推进。",
        "已终止": "确认终止原因并归档。",
        "已驳回": "按驳回意见修改后重新提交。",
    }
    action = actions.get(str(state), "人工确认当前状态与下一步。")

    if level == "W3":
        action = "W3 动作禁止自动执行，转人工或线下处理。"
        reasons.append("W3 无自动执行路径")
    elif level == "W2":
        reasons.append("W2 需要双人审批（reviewer + approver）")

    return Suggestion(
        suggestion=action,
        reasons=tuple(reasons[:4]),
        source="rule",
        model=RULE_MODEL,
        generated_at=datetime.now(UTC),
    )


def _parse_completion(payload: dict[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    try:
        content = payload["choices"][0]["message"]["content"]
        text = _CODE_FENCE.sub("", str(content).strip())
        data = json.loads(text)
        suggestion = str(data["suggestion"]).strip()
        reasons = tuple(
            str(item).strip() for item in data["reasons"] if str(item).strip()
        )
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not suggestion or not reasons:
        return None
    return suggestion, reasons[:3]


__all__ = [
    "LLM_TIMEOUT_SECONDS",
    "RULE_MODEL",
    "AssistantService",
    "RateLimiter",
    "Suggestion",
    "ticket_payload",
]
