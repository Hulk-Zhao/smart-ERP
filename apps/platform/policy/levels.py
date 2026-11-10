"""C4 策略契约：写入等级、动作声明与取严公式。

三条不可协商的规则（G-03 / FR-1406 / FR-1407）：
1. 等级在**代码**里声明（``ActionSpec.level`` / ``SceneSpec.max_level``），不读 YAML、不读库；
2. 生效等级一律 ``min(场景, 连接器, 动作, 熔断)``，配置只能把等级**降**下来；
3. W3 只用于识别与拒绝，永远不是可执行等级（``WriteGateway`` 无分支抛错）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class WriteLevel(IntEnum):
    W0 = 0  # 只读/建议
    W1 = 1  # 草稿类写入（可补偿）
    W2 = 2  # 需审批的写入
    W3 = 3  # 资金/过账/认证：只识别、只拒绝，永不执行

    @classmethod
    def parse(cls, value: "WriteLevel | int | str") -> "WriteLevel":
        if isinstance(value, WriteLevel):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return cls(value)
        if isinstance(value, str):
            raw = value.strip().upper()
            if raw in {"W0", "W1", "W2", "W3"}:
                return cls(int(raw[1]))
        raise ValueError(f"无法识别的写入等级: {value!r}")


# 任何 ActionSpec.writable_fields 命中这些字段 → 注册期断言失败（G-09 / NFR-S-04）
CRITICAL_READONLY_FIELDS: frozenset[str] = frozenset(
    {
        "stock_qty",
        "stock_amount",
        "gl_account",
        "posted_voucher",
        "bank_account",
        "payment_amount",
        "tax_rate",
    }
)

# 禁止动词表（FR-1407 验收）：SDK 里不允许出现这些动作
FORBIDDEN_ACTION_VERBS: frozenset[str] = frozenset(
    {
        "create_bank_account",
        "post_voucher",
        "certify_invoice",
        "execute_payment",
        "delete_document",
        "approve_document",
    }
)


@dataclass(frozen=True, slots=True)
class RateLimit:
    qps: float = 1.0
    daily_quota: int = 1_000
    concurrency: int = 1


@dataclass(frozen=True, slots=True)
class CompensateSpec:
    """补偿声明。缺省即「无补偿」，此时动作最高只能 W0（FR-1102）。"""

    kind: str  # delete_draft | release_reservation | cancel_request | red_invoice
    action_key: str | None = None


@dataclass(frozen=True, slots=True)
class VerifySpec:
    """写入后校验声明（§3.1.4）。无法声明 verify 的动作进不了注册表。"""

    kind: str  # read_by_external_id | read_by_business_key
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """连接器动作声明。等级、字段、补偿、校验全部由代码给出。"""

    key: str  # "{connector_alias}.{alias}.{action}"
    level: WriteLevel
    writable_fields: frozenset[str]
    forbidden_fields: frozenset[str] = frozenset()
    compensation: CompensateSpec | None = None
    verify: VerifySpec | None = None
    rate_limit: RateLimit = field(default_factory=RateLimit)
    idempotent: bool = True


@dataclass(frozen=True, slots=True)
class SceneSpec:
    """场景声明（AG-1..AG-8），上限是代码常量。"""

    code: str
    max_level: WriteLevel


def effective_level(
    *,
    scene_cap: WriteLevel | int | str,
    connector_cap: WriteLevel | int | str,
    action_level: WriteLevel | int | str,
    breaker_cap: WriteLevel | int | str,
) -> WriteLevel:
    """四路取严：W0–W2 之间取数值最小者，W3 是吸收元。

    - ``scene_cap`` / ``connector_cap`` / ``breaker_cap`` 都是「上限」语义，取最小值
      即最严；配置只能把动作等级往下压（W2 动作被连接器配置压到 W0），不能反向提升；
    - 任何一路是 W3（资金/过账/认证、或熔断到全禁）时结果必定 W3，不参与数值比较，
      否则 W3 会被更小的上限「压成」可执行等级。
    """

    levels = [
        WriteLevel.parse(scene_cap),
        WriteLevel.parse(connector_cap),
        WriteLevel.parse(action_level),
        WriteLevel.parse(breaker_cap),
    ]
    if any(level is WriteLevel.W3 for level in levels):
        return WriteLevel.W3
    return WriteLevel(min(levels))


def is_executable(level: WriteLevel | int | str) -> bool:
    """W3 不可执行；这里是唯一的判定口径。"""

    return WriteLevel.parse(level) is not WriteLevel.W3
