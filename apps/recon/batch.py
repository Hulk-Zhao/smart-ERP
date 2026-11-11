"""S2 对账批次生命周期与状态机（FR-101 / FR-102 / FR-108）。

状态词表逐字取自 PRD §6.2「批次」枚举，共 8 个：
已创建 / 已取数 / 已匹配 / 有差异 / 已平 / 待补数据 / 已关闭 / 已作废。

边界：
- 纯领域层：不连库、不调连接器、不读环境变量。输入是已归一的行数、差异数与
  来源可用性，输出是不可变批次快照。
- 批次关闭后数据与匹配结果不可原地修改；改判只能 ``reopen()``：旧版本置
  「已作废」但保留结论，新版本从「已创建」重新走生命周期（FR-108）。
- 缺失不等于零：来源不可读进「待补数据」并留下系统与责任人，不进入匹配
  （FR-102）；映射完整率 < 95% 直接拒绝创建并列出缺失映射（FR-101 / DQ-09）。
- 与 ``models.BatchStatus`` 区分：那个是单次对账的结论状态（无法对账 / 待人工复核 /
  已对账），本模块的 ``BatchState`` 是批次自身的生命周期状态，两者不可互相赋值。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

#: 主数据映射完整率下限（PRD DQ-09：低于 95% 阻断批次创建）
MAPPING_COMPLETENESS_MIN = Decimal("0.95")


class BatchState(str, Enum):
    """PRD §6.2 批次状态，不增不改。"""

    CREATED = "已创建"
    DATA_FETCHED = "已取数"
    MATCHED = "已匹配"
    HAS_DIFF = "有差异"
    BALANCED = "已平"
    MISSING_DATA = "待补数据"
    CLOSED = "已关闭"
    VOID = "已作废"


#: 状态迁移白名单。``CLOSED -> VOID`` 只允许由 ``Batch.reopen()`` 触发：
#: 作废旧版本必须同时新建版本，不能单独把已关闭批次改成已作废。
TRANSITIONS: dict[BatchState, frozenset[BatchState]] = {
    BatchState.CREATED: frozenset(
        {BatchState.DATA_FETCHED, BatchState.MISSING_DATA, BatchState.VOID}
    ),
    BatchState.MISSING_DATA: frozenset({BatchState.DATA_FETCHED, BatchState.VOID}),
    BatchState.DATA_FETCHED: frozenset(
        {BatchState.MATCHED, BatchState.MISSING_DATA, BatchState.VOID}
    ),
    BatchState.MATCHED: frozenset(
        {BatchState.HAS_DIFF, BatchState.BALANCED, BatchState.VOID}
    ),
    BatchState.HAS_DIFF: frozenset({BatchState.BALANCED, BatchState.VOID}),
    BatchState.BALANCED: frozenset({BatchState.CLOSED, BatchState.VOID}),
    BatchState.CLOSED: frozenset({BatchState.VOID}),
    BatchState.VOID: frozenset(),
}

BATCH_STATES: tuple[str, ...] = tuple(state.value for state in BatchState)

TRANSITION_EDGES: frozenset[tuple[str, str]] = frozenset(
    (source.value, target.value)
    for source, targets in TRANSITIONS.items()
    for target in targets
)


class BatchError(Exception):
    """领域错误；``code`` 复用 ``contracts/errors`` 的冻结错误码。"""

    code = "E-STATE-ILLEGAL"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class BatchValidationError(BatchError):
    """入参不完整或类型不合法。"""

    code = "E-DATA-MISSING"


class IllegalBatchTransition(BatchError):
    """非法状态迁移。"""

    code = "E-STATE-ILLEGAL"


class MappingIncompleteError(BatchError):
    """主数据映射完整率低于 95%。"""

    code = "E-DATA-UNBOUND"

    def __init__(self, missing: Iterable[str] = ()) -> None:
        self.missing = tuple(missing)
        detail = "、".join(self.missing) or "供应商/物料映射"
        super().__init__(
            f"主数据映射完整率低于 {MAPPING_COMPLETENESS_MIN:.0%}，"
            f"拒绝创建批次；缺失映射：{detail}"
        )


class UnclosedBatchError(BatchError):
    """同供应商 × 期间已有未关闭批次。"""

    code = "E-IDEM-CONFLICT"

    def __init__(self, batch: Batch) -> None:
        self.batch = batch
        super().__init__(
            f"供应商 {batch.supplier_code} 期间 {batch.period} 已有未关闭批次 "
            f"{batch.batch_no}（{batch.state.value}）；"
            "请先合并或作废旧批次（需理由），不要重复创建"
        )


class ClosedBatchError(BatchError):
    """批次已关闭，不能新建或直接作废。"""

    code = "E-STATE-ILLEGAL"


class OpenDiffError(BatchError):
    """仍有未关闭差异，不能关闭批次。"""

    code = "E-STATE-ILLEGAL"


class ReopenPermissionError(BatchError):
    """重开权限或确认人不满足。"""

    code = "E-POLICY-APPROVAL"


class ReopenBlockedError(BatchError):
    """存在外部已生效对象，必须先撤销/撤回。"""

    code = "E-EXTERNAL-EFFECTIVE"


class BatchNotFoundError(BatchError):
    """批次不存在。"""

    code = "E-RESOURCE-NOTFOUND"


def assert_transition(current: BatchState | str, target: BatchState | str) -> None:
    """纯白名单校验；带业务前置条件的判断在 ``Batch`` 的方法里。"""

    source = BatchState(current)
    nxt = BatchState(target)
    if nxt not in TRANSITIONS[source]:
        raise IllegalBatchTransition(
            f"{source.value} -> {nxt.value} 不在批次状态迁移白名单内"
        )


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchValidationError(f"{field_name} 不能为空")
    return value.strip()


def _require_aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise BatchValidationError(f"{field_name} 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise BatchValidationError(f"{field_name} 必须带时区")
    return value


def _require_count(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BatchValidationError(f"{field_name} 必须是非负整数")
    return value


def _as_decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BatchValidationError(f"{field_name} 不是合法数字：{value!r}") from exc
    if not result.is_finite():
        raise BatchValidationError(f"{field_name} 不是有限数字：{value!r}")
    return result


def _batch_no(batch_id: UUID) -> str:
    return f"RB-{batch_id.hex[:12].upper()}"


@dataclass(frozen=True, slots=True)
class MissingSource:
    """缺失来源：明确到系统与责任人（FR-102）。"""

    system: str
    owner: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "system", _require_text(self.system, "缺失来源系统"))

    def to_dict(self) -> dict:
        return {"system": self.system, "owner": self.owner, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """一次取数的来源结果；``readable=False`` 即该来源缺失。"""

    system: str
    readable: bool
    owner: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "system", _require_text(self.system, "来源系统"))


@dataclass(frozen=True, slots=True)
class ConclusionSnapshot:
    """关闭时由调用方提供的对账结论数字（不含版本与关闭人）。"""

    matched: int
    supplier_lines: int
    diff_count: int
    diff_amount: Decimal
    conclusive: bool
    status_reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "matched", _require_count(self.matched, "matched"))
        object.__setattr__(
            self,
            "supplier_lines",
            _require_count(self.supplier_lines, "supplier_lines"),
        )
        object.__setattr__(
            self, "diff_count", _require_count(self.diff_count, "diff_count")
        )
        amount = _as_decimal(self.diff_amount, "diff_amount")
        if amount < 0:
            raise BatchValidationError("diff_amount 不能为负")
        object.__setattr__(self, "diff_amount", amount)
        object.__setattr__(
            self, "status_reason", _require_text(self.status_reason, "status_reason")
        )


@dataclass(frozen=True, slots=True)
class BatchConclusion:
    """关闭时固化的结论快照；旧版本作废后仍保留。"""

    version: int
    matched: int
    supplier_lines: int
    diff_count: int
    diff_amount: Decimal
    conclusive: bool
    status_reason: str
    closed_by: str
    closed_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1:
            raise BatchValidationError("结论版本号必须 >= 1")
        for name in ("matched", "supplier_lines", "diff_count"):
            object.__setattr__(self, name, _require_count(getattr(self, name), name))
        amount = _as_decimal(self.diff_amount, "diff_amount")
        if amount < 0:
            raise BatchValidationError("diff_amount 不能为负")
        object.__setattr__(self, "diff_amount", amount)
        object.__setattr__(
            self, "status_reason", _require_text(self.status_reason, "status_reason")
        )
        object.__setattr__(
            self, "closed_by", _require_text(self.closed_by, "closed_by")
        )
        object.__setattr__(
            self, "closed_at", _require_aware(self.closed_at, "closed_at")
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "matched": self.matched,
            "supplier_lines": self.supplier_lines,
            "diff_count": self.diff_count,
            "diff_amount": f"{self.diff_amount:.4f}",
            "conclusive": self.conclusive,
            "status_reason": self.status_reason,
            "closed_by": self.closed_by,
            "closed_at": self.closed_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Batch:
    """不可变批次快照；所有状态变化都返回新对象。"""

    batch_id: UUID
    batch_no: str
    tenant_id: UUID
    org_id: str
    period: str
    supplier_code: str
    system_ids: tuple[str, ...]
    version: int
    state: BatchState
    input_fingerprint: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    estimated_lines: int = 0
    missing_sources: tuple[MissingSource, ...] = ()
    open_diff_count: int = 0
    conclusion: BatchConclusion | None = None
    void_reason: str | None = None
    supersedes: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.batch_id, UUID):
            raise BatchValidationError("batch_id 必须是 UUID")
        if not isinstance(self.tenant_id, UUID):
            raise BatchValidationError("tenant_id 必须是 UUID")
        object.__setattr__(self, "state", BatchState(self.state))
        for name in (
            "batch_no",
            "org_id",
            "period",
            "supplier_code",
            "input_fingerprint",
            "created_by",
        ):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        systems = tuple(self.system_ids)
        if not systems:
            raise BatchValidationError("system_ids 不能为空")
        object.__setattr__(self, "system_ids", systems)
        if self.version < 1:
            raise BatchValidationError("版本号必须 >= 1")
        if self.version == 1 and self.supersedes is not None:
            raise BatchValidationError("版本 1 不能声明 supersedes")
        if self.version > 1 and self.supersedes is None:
            raise BatchValidationError("版本 > 1 必须声明 supersedes")
        object.__setattr__(
            self,
            "estimated_lines",
            _require_count(self.estimated_lines, "estimated_lines"),
        )
        object.__setattr__(
            self,
            "open_diff_count",
            _require_count(self.open_diff_count, "open_diff_count"),
        )
        object.__setattr__(self, "missing_sources", tuple(self.missing_sources))
        object.__setattr__(
            self, "created_at", _require_aware(self.created_at, "created_at")
        )
        object.__setattr__(
            self, "updated_at", _require_aware(self.updated_at, "updated_at")
        )
        if self.updated_at < self.created_at:
            raise BatchValidationError("updated_at 不能早于 created_at")
        if self.state is BatchState.CLOSED and self.conclusion is None:
            raise BatchValidationError("已关闭批次必须有结论快照")
        if self.state is BatchState.VOID and not (self.void_reason or "").strip():
            raise BatchValidationError("已作废批次必须写明理由")
        if self.conclusion is not None and self.conclusion.version != self.version:
            raise BatchValidationError("结论快照版本号与批次版本不一致")

    # -- 状态推进 ----------------------------------------------------------

    def transition(
        self,
        target: BatchState | str,
        *,
        actor: str,
        at: datetime,
        reason: str | None = None,
    ) -> Batch:
        """按白名单推进状态；带前置条件的生命周期操作见下方方法。"""

        actor = _require_text(actor, "操作人")
        at = _require_aware(at, "操作时间")
        nxt = BatchState(target)
        if self.state is BatchState.CLOSED and nxt is BatchState.VOID:
            raise IllegalBatchTransition(
                "已关闭批次不能直接作废；重开必须走 reopen()，旧版本置作废并新建版本"
            )
        assert_transition(self.state, nxt)
        if nxt is BatchState.VOID:
            reason = _require_text(reason, "作废理由")
        return replace(
            self,
            state=nxt,
            updated_at=at,
            void_reason=reason if nxt is BatchState.VOID else self.void_reason,
        )

    def _require_editable(self, action: str) -> None:
        if self.state in {BatchState.CLOSED, BatchState.VOID}:
            raise IllegalBatchTransition(
                f"批次 {self.batch_no} 已{self.state.value}，不可再{action}；"
                "改判请走 reopen()"
            )

    def mark_sources(
        self,
        statuses: Iterable[SourceStatus],
        *,
        actor: str,
        at: datetime,
    ) -> Batch:
        """登记取数结果：任一来源不可读 → 待补数据，不进入匹配（FR-102）。"""

        self._require_editable("更新取数结果")
        actor = _require_text(actor, "操作人")
        at = _require_aware(at, "操作时间")
        items = tuple(statuses)
        if not items:
            raise BatchValidationError("取数结果不能为空")
        missing = tuple(
            MissingSource(item.system, item.owner, item.detail)
            for item in items
            if not item.readable
        )
        target = BatchState.MISSING_DATA if missing else BatchState.DATA_FETCHED
        if self.state is BatchState.MISSING_DATA and target is BatchState.MISSING_DATA:
            return replace(self, missing_sources=missing, updated_at=at)
        advanced = self.transition(target, actor=actor, at=at)
        return replace(advanced, missing_sources=missing, updated_at=at)

    def mark_matched(self, *, actor: str, at: datetime) -> Batch:
        """取数完成 → 已匹配（FR-103 前置 = 已取数）。"""

        self._require_editable("进入匹配")
        return self.transition(BatchState.MATCHED, actor=actor, at=at)

    def record_diff_outcome(
        self, open_diff_count: int, *, actor: str, at: datetime
    ) -> Batch:
        """匹配完成后落到「有差异」或「已平」；差异数必须由调用方给出。"""

        self._require_editable("记录差异结论")
        count = _require_count(open_diff_count, "open_diff_count")
        if self.state is not BatchState.MATCHED:
            raise IllegalBatchTransition(
                f"只有已匹配批次可记录差异结论，当前 {self.state.value}"
            )
        target = BatchState.HAS_DIFF if count else BatchState.BALANCED
        advanced = self.transition(target, actor=actor, at=at)
        return replace(advanced, open_diff_count=count)

    def settle_diffs(self, *, actor: str, at: datetime) -> Batch:
        """全部差异关闭 → 已平；关闭仍须再调用 ``close()``。"""

        self._require_editable("结清差异")
        if self.state is not BatchState.HAS_DIFF:
            raise IllegalBatchTransition(
                f"只有有差异批次可结清差异，当前 {self.state.value}"
            )
        advanced = self.transition(BatchState.BALANCED, actor=actor, at=at)
        return replace(advanced, open_diff_count=0)

    def close(self, snapshot: ConclusionSnapshot, *, actor: str, at: datetime) -> Batch:
        """关闭批次并固化结论（FR-108）：必须已平且无未关闭差异。"""

        self._require_editable("关闭")
        actor = _require_text(actor, "关闭人")
        at = _require_aware(at, "关闭时间")
        if self.state is BatchState.HAS_DIFF or self.open_diff_count:
            raise OpenDiffError(
                f"批次 {self.batch_no} 仍有 {self.open_diff_count} 条未关闭差异，不能关闭"
            )
        if self.state is not BatchState.BALANCED:
            raise IllegalBatchTransition(f"只有已平批次可关闭，当前 {self.state.value}")
        conclusion = BatchConclusion(
            version=self.version,
            matched=snapshot.matched,
            supplier_lines=snapshot.supplier_lines,
            diff_count=snapshot.diff_count,
            diff_amount=snapshot.diff_amount,
            conclusive=snapshot.conclusive,
            status_reason=snapshot.status_reason,
            closed_by=actor,
            closed_at=at,
        )
        return replace(
            self, state=BatchState.CLOSED, conclusion=conclusion, updated_at=at
        )

    def reopen(
        self,
        *,
        reason: str,
        actor: str,
        at: datetime,
        senior: bool,
        approver: str | None,
        payment_draft_count: int = 0,
        external_confirmation_sent: bool = False,
        input_fingerprint: str | None = None,
    ) -> tuple[Batch, Batch]:
        """重开（FR-108）：旧版本作废但保留结论，新版本从已创建开始。

        前置（缺一即拒绝）：
        - 仅已关闭批次可重开；
        - 理由非空、操作人具备高级权限；
        - 确认人必须存在，且不能是原发起人，也不能是重开操作人本人；
        - 已生成付款草稿的须先撤销；已对外发送确认的须先人工撤回。
        """

        reason = _require_text(reason, "重开理由")
        actor = _require_text(actor, "重开操作人")
        at = _require_aware(at, "重开时间")
        if self.state is not BatchState.CLOSED:
            raise IllegalBatchTransition(
                f"只有已关闭批次可重开，当前 {self.state.value}"
            )
        if not senior:
            raise ReopenPermissionError("重开需要高级权限")
        if not isinstance(approver, str) or not approver.strip():
            raise ReopenPermissionError("重开必须由非发起人确认")
        approver = approver.strip()
        if approver == self.created_by:
            raise ReopenPermissionError("重开必须由非发起人确认")
        if approver == actor:
            raise ReopenPermissionError("重开操作人与确认人不得为同一人")
        if _require_count(payment_draft_count, "payment_draft_count"):
            raise ReopenBlockedError("该批次已生成付款草稿，必须先撤销草稿再重开")
        if external_confirmation_sent:
            raise ReopenBlockedError("该批次已对外发送对账确认，需人工确认撤回后再重开")

        old = replace(
            self,
            state=BatchState.VOID,
            updated_at=at,
            void_reason=f"重开：{reason}",
        )
        new = Batch(
            batch_id=uuid4(),
            batch_no=self.batch_no,
            tenant_id=self.tenant_id,
            org_id=self.org_id,
            period=self.period,
            supplier_code=self.supplier_code,
            system_ids=self.system_ids,
            version=self.version + 1,
            state=BatchState.CREATED,
            input_fingerprint=_require_text(
                input_fingerprint or self.input_fingerprint, "input_fingerprint"
            ),
            created_by=actor,
            created_at=at,
            updated_at=at,
            estimated_lines=self.estimated_lines,
            supersedes=self.batch_id,
        )
        return old, new

    def to_dict(self) -> dict:
        return {
            "batch_id": str(self.batch_id),
            "batch_no": self.batch_no,
            "tenant_id": str(self.tenant_id),
            "org_id": self.org_id,
            "period": self.period,
            "supplier_code": self.supplier_code,
            "system_ids": list(self.system_ids),
            "version": self.version,
            "state": self.state.value,
            "input_fingerprint": self.input_fingerprint,
            "estimated_lines": self.estimated_lines,
            "missing_sources": [item.to_dict() for item in self.missing_sources],
            "open_diff_count": self.open_diff_count,
            "conclusion": None
            if self.conclusion is None
            else self.conclusion.to_dict(),
            "void_reason": self.void_reason,
            "supersedes": None if self.supersedes is None else str(self.supersedes),
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """生成批次入参：一个供应商 × 一个期间 = 一个批次（FR-101 验收 52 家）。"""

    tenant_id: UUID
    org_id: str
    period: str
    suppliers: tuple[str, ...]
    system_ids: tuple[str, ...]
    mapping_completeness: Decimal
    missing_mappings: tuple[str, ...] = ()
    estimated_lines: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, UUID):
            raise BatchValidationError("tenant_id 必须是 UUID")
        object.__setattr__(self, "org_id", _require_text(self.org_id, "org_id"))
        object.__setattr__(self, "period", _require_text(self.period, "period"))
        suppliers = tuple(_require_text(item, "供应商编码") for item in self.suppliers)
        if not suppliers:
            raise BatchValidationError("至少选择一个供应商")
        if len(set(suppliers)) != len(suppliers):
            raise BatchValidationError("供应商编码不能重复")
        object.__setattr__(self, "suppliers", suppliers)
        systems = tuple(_require_text(item, "系统标识") for item in self.system_ids)
        if not systems:
            raise BatchValidationError("system_ids 不能为空")
        if len(set(systems)) != len(systems):
            raise BatchValidationError("system_ids 不能重复")
        object.__setattr__(self, "system_ids", systems)
        completeness = _as_decimal(self.mapping_completeness, "mapping_completeness")
        if not Decimal(0) <= completeness <= Decimal(1):
            raise BatchValidationError("mapping_completeness 必须在 0..1 之间")
        object.__setattr__(self, "mapping_completeness", completeness)
        mappings = tuple(
            _require_text(item, "缺失映射") for item in self.missing_mappings
        )
        object.__setattr__(self, "missing_mappings", mappings)
        lines = {
            _require_text(key, "供应商编码"): _require_count(value, "estimated_lines")
            for key, value in dict(self.estimated_lines).items()
        }
        object.__setattr__(self, "estimated_lines", lines)


class BatchService:
    """进程内批次生命周期仓库；持久化由上层适配，不在这里落库。"""

    def __init__(self) -> None:
        self._batches: dict[UUID, Batch] = {}
        self._current: dict[tuple[UUID, str, str, str], UUID] = {}
        self._by_no: dict[str, list[UUID]] = {}

    # -- 生成 --------------------------------------------------------------

    def generate(
        self, request: GenerationRequest, *, actor: str, at: datetime
    ) -> tuple[Batch, ...]:
        """按供应商逐个生成批次；同请求重复发起返回原批次（FR-101）。"""

        actor = _require_text(actor, "创建人")
        _require_aware(at, "创建时间")
        if request.mapping_completeness < MAPPING_COMPLETENESS_MIN:
            raise MappingIncompleteError(request.missing_mappings)

        # 先整批校验冲突，避免 52 家里前几家已建、后几家报错留下半批数据。
        planned: list[tuple[str, str, Batch | None]] = []
        for supplier in request.suppliers:
            fingerprint = self._fingerprint(request, supplier)
            key = (request.tenant_id, request.org_id, request.period, supplier)
            current_id = self._current.get(key)
            existing = None if current_id is None else self._batches[current_id]
            if existing is not None and existing.input_fingerprint != fingerprint:
                if existing.state is BatchState.CLOSED:
                    raise ClosedBatchError(
                        f"供应商 {supplier} 期间 {request.period} 的批次 "
                        f"{existing.batch_no} 已关闭；改数据请重开，不要新建批次"
                    )
                raise UnclosedBatchError(existing)
            planned.append((supplier, fingerprint, existing))

        generated: list[Batch] = []
        for supplier, fingerprint, existing in planned:
            if existing is not None:
                generated.append(existing)
                continue

            batch_id = uuid4()
            batch = Batch(
                batch_id=batch_id,
                batch_no=_batch_no(batch_id),
                tenant_id=request.tenant_id,
                org_id=request.org_id,
                period=request.period,
                supplier_code=supplier,
                system_ids=request.system_ids,
                version=1,
                state=BatchState.CREATED,
                input_fingerprint=fingerprint,
                created_by=actor,
                created_at=at,
                updated_at=at,
                estimated_lines=request.estimated_lines.get(supplier, 0),
            )
            self._store(batch)
            generated.append(batch)
        return tuple(generated)

    @staticmethod
    def _fingerprint(request: GenerationRequest, supplier: str) -> str:
        payload = "\x1f".join(
            [
                str(request.tenant_id),
                request.org_id,
                request.period,
                supplier,
                ",".join(request.system_ids),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # -- 生命周期操作 ------------------------------------------------------

    def void(self, batch_id: UUID, *, reason: str, actor: str, at: datetime) -> Batch:
        batch = self.get(batch_id)
        if batch.state is BatchState.CLOSED:
            raise ClosedBatchError(f"批次 {batch.batch_no} 已关闭；需重开而非作废")
        voided = batch.transition(BatchState.VOID, actor=actor, at=at, reason=reason)
        self._store(voided)
        return voided

    def mark_sources(
        self,
        batch_id: UUID,
        statuses: Iterable[SourceStatus],
        *,
        actor: str,
        at: datetime,
    ) -> Batch:
        return self._update(
            batch_id, lambda batch: batch.mark_sources(statuses, actor=actor, at=at)
        )

    def mark_matched(self, batch_id: UUID, *, actor: str, at: datetime) -> Batch:
        return self._update(
            batch_id, lambda batch: batch.mark_matched(actor=actor, at=at)
        )

    def record_diff_outcome(
        self,
        batch_id: UUID,
        open_diff_count: int,
        *,
        actor: str,
        at: datetime,
    ) -> Batch:
        return self._update(
            batch_id,
            lambda batch: batch.record_diff_outcome(
                open_diff_count, actor=actor, at=at
            ),
        )

    def settle_diffs(self, batch_id: UUID, *, actor: str, at: datetime) -> Batch:
        return self._update(
            batch_id, lambda batch: batch.settle_diffs(actor=actor, at=at)
        )

    def close(
        self,
        batch_id: UUID,
        snapshot: ConclusionSnapshot,
        *,
        actor: str,
        at: datetime,
    ) -> Batch:
        return self._update(
            batch_id, lambda batch: batch.close(snapshot, actor=actor, at=at)
        )

    def reopen(
        self,
        batch_id: UUID,
        *,
        reason: str,
        actor: str,
        at: datetime,
        senior: bool,
        approver: str | None,
        payment_draft_count: int = 0,
        external_confirmation_sent: bool = False,
        input_fingerprint: str | None = None,
    ) -> Batch:
        batch = self.get(batch_id)
        old, new = batch.reopen(
            reason=reason,
            actor=actor,
            at=at,
            senior=senior,
            approver=approver,
            payment_draft_count=payment_draft_count,
            external_confirmation_sent=external_confirmation_sent,
            input_fingerprint=input_fingerprint,
        )
        self._batches[old.batch_id] = old
        self._store(new)
        self._current[self._key(new)] = new.batch_id
        return new

    # -- 查询 --------------------------------------------------------------

    def get(self, batch_id: UUID) -> Batch:
        try:
            return self._batches[UUID(str(batch_id))]
        except (KeyError, ValueError) as exc:
            raise BatchNotFoundError(f"批次 {batch_id} 不存在") from exc

    def history(self, batch_no: str) -> tuple[Batch, ...]:
        ids = self._by_no.get(batch_no, [])
        return tuple(
            sorted(
                (self._batches[item] for item in ids), key=lambda batch: batch.version
            )
        )

    def current(
        self, tenant_id: UUID, org_id: str, period: str, supplier_code: str
    ) -> Batch | None:
        batch_id = self._current.get((tenant_id, org_id, period, supplier_code))
        return None if batch_id is None else self._batches[batch_id]

    # -- 内部 --------------------------------------------------------------

    def _update(self, batch_id: UUID, action) -> Batch:
        updated = action(self.get(batch_id))
        self._store(updated)
        return updated

    @staticmethod
    def _key(batch: Batch) -> tuple[UUID, str, str, str]:
        return (
            batch.tenant_id,
            batch.org_id,
            batch.period,
            batch.supplier_code,
        )

    def _store(self, batch: Batch) -> None:
        self._batches[batch.batch_id] = batch
        key = self._key(batch)
        if batch.state is BatchState.VOID:
            if self._current.get(key) == batch.batch_id:
                self._current.pop(key, None)
        else:
            self._current[key] = batch.batch_id
        versions = self._by_no.setdefault(batch.batch_no, [])
        if batch.batch_id not in versions:
            versions.append(batch.batch_id)


__all__ = [
    "BATCH_STATES",
    "MAPPING_COMPLETENESS_MIN",
    "TRANSITIONS",
    "TRANSITION_EDGES",
    "Batch",
    "BatchConclusion",
    "BatchError",
    "BatchNotFoundError",
    "BatchService",
    "BatchState",
    "BatchValidationError",
    "ClosedBatchError",
    "ConclusionSnapshot",
    "GenerationRequest",
    "IllegalBatchTransition",
    "MappingIncompleteError",
    "MissingSource",
    "OpenDiffError",
    "ReopenBlockedError",
    "ReopenPermissionError",
    "SourceStatus",
    "UnclosedBatchError",
    "assert_transition",
]
