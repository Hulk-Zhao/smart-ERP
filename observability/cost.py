"""成本归因：按工单 / Agent / 步骤累计调用次数与耗时。

硬规则：**没有实测 token 就不写 token**。字段保持 ``None``，汇总时用
``token_coverage`` 明确「多少调用有实测值」，绝不按平均单价反推或补零。
未来接入模型调用时，只需把真实 ``input_tokens`` / ``output_tokens`` /
``cost_micros`` 传进来，行结构已与 ``llm_usage`` 表对齐（tech 01 §3.7.3）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from observability.trace import normalize


class UsageError(ValueError):
    """用量记录非法（负数、缺工单等）。"""


def _non_negative(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageError(f"{name} 必须是非负整数，实际 {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class UsageRecord:
    ticket_id: str
    agent: str
    step_no: int | None = None
    scene_code: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_micros: int | None = None
    price_version: str | None = None
    calls: int = 1
    duration_ms: float = 0.0
    trace_id: str | None = None
    recorded_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    def __post_init__(self) -> None:
        if not self.ticket_id.strip():
            raise UsageError("用量必须归属到工单（I-1）")
        if not self.agent.strip():
            raise UsageError("用量必须记录 Agent 名称")
        if self.step_no is not None and self.step_no < 1:
            raise UsageError(f"step_no 从 1 开始，实际 {self.step_no!r}")
        if self.calls < 1:
            raise UsageError("calls 至少为 1")
        if self.duration_ms < 0:
            raise UsageError("duration_ms 不得为负")
        for name in ("input_tokens", "output_tokens", "cost_micros"):
            _non_negative(getattr(self, name), name)

    @property
    def tokens_measured(self) -> bool:
        """只有出入 token 都有实测值，才算一次「可计价」调用。"""

        return self.input_tokens is not None and self.output_tokens is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "agent": self.agent,
            "step_no": self.step_no,
            "scene_code": self.scene_code,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_micros": self.cost_micros,
            "price_version": self.price_version,
            "calls": self.calls,
            "duration_ms": self.duration_ms,
            "trace_id": self.trace_id,
            "recorded_at": self.recorded_at.isoformat(),
        }

    def to_llm_usage_row(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        """映射到 ``llm_usage`` 表字段；未实测的列写 ``None``，不补零。"""

        return {
            "tenant_id": tenant_id,
            "ticket_id": self.ticket_id,
            "scene_code": self.scene_code,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_micros": self.cost_micros,
            "price_version": self.price_version,
            "created_at": self.recorded_at,
        }


class CostLedger:
    """进程内用量台账；线程安全，只追加。"""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []
        self._lock = threading.Lock()

    @property
    def records(self) -> tuple[UsageRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def record_call(
        self,
        *,
        ticket_id: str,
        agent: str,
        step_no: int | None = None,
        calls: int = 1,
        duration_ms: float = 0.0,
        scene_code: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_micros: int | None = None,
        price_version: str | None = None,
        trace_id: str | None = None,
    ) -> UsageRecord:
        record = UsageRecord(
            ticket_id=ticket_id,
            agent=agent,
            step_no=step_no,
            scene_code=scene_code,
            model=model,
            prompt_version=prompt_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micros=cost_micros,
            price_version=price_version,
            calls=calls,
            duration_ms=duration_ms,
            trace_id=trace_id,
        )
        with self._lock:
            self._records.append(record)
        return record

    def record_llm_call(
        self,
        *,
        ticket_id: str,
        agent: str,
        model: str,
        prompt_version: str,
        step_no: int | None = None,
        duration_ms: float = 0.0,
        scene_code: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_micros: int | None = None,
        price_version: str | None = None,
        trace_id: str | None = None,
        calls: int = 1,
    ) -> UsageRecord:
        """模型调用占位：``model`` / ``prompt_version`` 必填，token 可暂缺。"""

        if not model.strip():
            raise UsageError("模型调用必须记录 model")
        if not prompt_version.strip():
            raise UsageError("模型调用必须记录 prompt_version（漂移归因）")
        return self.record_call(
            ticket_id=ticket_id,
            agent=agent,
            step_no=step_no,
            calls=calls,
            duration_ms=duration_ms,
            scene_code=scene_code,
            model=model,
            prompt_version=prompt_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micros=cost_micros,
            price_version=price_version,
            trace_id=trace_id,
        )

    def summary(self) -> list[dict[str, Any]]:
        """按 (工单, Agent, 步骤) 汇总；未实测项给覆盖度，不做外推。"""

        grouped: dict[tuple[str, str, int | None], list[UsageRecord]] = {}
        for record in self.records:
            grouped.setdefault((record.ticket_id, record.agent, record.step_no), []).append(
                record
            )

        rows: list[dict[str, Any]] = []
        for (ticket_id, agent, step_no), items in sorted(
            grouped.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or 0)
        ):
            total_calls = sum(item.calls for item in items)
            measured = [item for item in items if item.tokens_measured]
            measured_calls = sum(item.calls for item in measured)
            rows.append(
                {
                    "ticket_id": ticket_id,
                    "agent": agent,
                    "step_no": step_no,
                    "calls": total_calls,
                    "duration_ms": round(sum(item.duration_ms for item in items), 3),
                    "models": sorted({item.model for item in items if item.model}),
                    "scenes": sorted({item.scene_code for item in items if item.scene_code}),
                    "input_tokens": (
                        sum(item.input_tokens or 0 for item in measured) if measured else None
                    ),
                    "output_tokens": (
                        sum(item.output_tokens or 0 for item in measured) if measured else None
                    ),
                    "cost_micros": (
                        sum(item.cost_micros or 0 for item in measured) if measured else None
                    ),
                    "token_coverage": f"{measured_calls}/{total_calls}",
                }
            )
        return rows

    def llm_usage_rows(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """只导出真正的模型调用（``model`` 非空），结构对齐 ``llm_usage``。"""

        return [
            record.to_llm_usage_row(tenant_id=tenant_id)
            for record in self.records
            if record.model
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [normalize(record.to_dict()) for record in self.records],
            "summary": self.summary(),
        }
