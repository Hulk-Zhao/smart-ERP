"""结果计费的计量表（W0 只读）：只输出程序性事实，不判断结论是否正确。

为什么单独成模块：计量口径（哪些行计费、单价从哪来）必须与路由解耦、可单测，
且**不得**在本模块内写库、碰资金、改状态。所有数据来自对账批次的内存投影；
「已确认错放行 / 客户原因 / 是否被人改过 / 工单号 / 前像后像」在当前数据模型里
没有任何来源，一律返回 ``None`` 并在 ``data_gaps`` 里写明缺什么，绝不用默认值
伪装成已知事实。

计费口径（可在响应 ``calculation`` 中逐条核对）::

    应处理行数 = 对账引擎的供应商对账单行数（metrics.supplier_lines）
    自动放行行数 = 引擎状态「已匹配」且无需人工
    争议挂起行数 = 引擎标记需人工（待人工/匹配歧义/候选待人工）
    计费行数（billable_lines）= billable 桶计数 = 自动放行 − 已确认错放行 − 客户原因不计费
    本期金额 = 计费行数 × 单行单价（单价只从配置读）
    累计金额 = 已计金额 + 本期金额
    封顶余额 = 封顶金额 − 累计金额

「有效对账行」是对账引擎的口径（行级自动，含已自动归因的差异行），与本模块的
``billable_lines`` 不是同一个数；两者不得互相代称（同一批次实测 64 vs 59）。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from apps.api.recon_snapshot import BatchSnapshot, SnapshotLine, UnitPriceSource
from apps.api.schemas import (
    BillingBucket,
    EvidenceOut,
    MeteringDataGapOut,
    MeteringExcludedOut,
    MeteringLineOut,
    MeteringStatementOut,
)
from apps.recon.models import LineStatus

#: 单价必须来自配置；这里只登记键名，不提供任何默认单价。
ENV_UNIT_PRICE = "ERP_METERING_UNIT_PRICE"
ENV_CURRENCY = "ERP_METERING_CURRENCY"
ENV_UNIT_LABEL = "ERP_METERING_UNIT_LABEL"
ENV_CAP_AMOUNT = "ERP_METERING_CAP_AMOUNT"
ENV_PRIOR_AMOUNT = "ERP_METERING_PRIOR_AMOUNT"

MONEY_EXP = Decimal("0.0001")
DEFAULT_UNIT_LABEL = "行"

#: 取价口径：某时点的价 = ``effective_from <= t`` 中**最大**的那一行。
#:
#: **不按 ``supersedes_id`` 过滤**。``supersedes_id`` 是审计追溯链
#: （「这行替代了哪一行」），不是取价条件：
#: - 若按它排除旧行，``t`` 早于新价生效日的历史账就会查不到当时有效的价；
#: - 纠错靠新行把 ``effective_from`` 往后推、自然遮蔽旧行；
#: - 已结账的历史金额由 ``recon_batch`` 的单价/币种/价目快照保证不变。
#:
#: 币种必须与批次一致；不匹配即视为「无有效价」（fail closed），
#: 绝不静默改用价格表里的另一种币种。
PRICE_LOOKUP_SQL = """
SELECT id, unit_price, currency
  FROM erp.metering_price_book
 WHERE tenant_id = CAST(:tenant_id AS uuid)
   AND currency = :currency
   AND effective_from <= CAST(:at AS timestamptz)
 ORDER BY effective_from DESC
 LIMIT 1
"""

#: 供应商侧「非自动放行、也非待人工」的状态 → 对外不计费原因码。
OTHER_EXCLUSION_REASONS: dict[LineStatus, str] = {
    LineStatus.UNMATCHED_SUPPLIER: "unmatched_supplier",
    LineStatus.UNMATCHED_OURS: "unmatched_ours",
    LineStatus.DUPLICATE: "duplicate_billing",
    LineStatus.UNPARSEABLE: "unparseable",
}
OTHER_EXCLUSION_REASONS_BY_VALUE = {
    status.value: code for status, code in OTHER_EXCLUSION_REASONS.items()
}

#: CSV 逐行明细列（与响应字段一一对应；测试按此表断言表头）。
METERING_LINE_COLUMNS = (
    "line_no",
    "side",
    "supplier_code",
    "our_ref",
    "supplier_ref",
    "status",
    "billing_bucket",
    "auto_released",
    "needs_manual",
    "human_modified",
    "ticket_no",
    "before_ref",
    "after_ref",
    "excluded_reason",
    "diff_types",
    "owner",
    "confidence",
    "source_refs",
    "evidence",
)


def classify_billing_bucket(*, status: str, needs_manual: bool) -> BillingBucket:
    """计费桶的唯一分类函数：计数、行输出、差异过滤都必须调用它，禁止第二处重算。

    - ``billable``：引擎自动放行且计入计费行（当前扣减源未接入，自动放行即计费）；
    - ``auto_released``：引擎自动放行但被扣减不计费（「已确认错放行 / 客户原因」接入后才会出现）；
    - ``dispute_hold``：需人工（待人工/匹配歧义/候选待人工）；
    - ``other_unbillable``：其余供应商侧行（未匹配/重复计费/不可解析）。
    """

    if status == LineStatus.MATCHED.value:
        return BillingBucket.BILLABLE
    if needs_manual:
        return BillingBucket.DISPUTE_HOLD
    return BillingBucket.OTHER_UNBILLABLE


class MeteringConfigError(ValueError):
    """计量配置缺失/非法：宁可拒绝出账，也不用默认值硬编码单价。"""


@dataclass(frozen=True, slots=True)
class MeteringPolicy:
    """单行单价、封顶与已计金额；全部来自环境配置（``ERP_METERING_*``）。"""

    unit_price: Decimal
    currency: str = "CNY"
    unit_label: str = DEFAULT_UNIT_LABEL
    cap_amount: Decimal | None = None
    prior_amount: Decimal = Decimal(0)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MeteringPolicy:
        source = os.environ if env is None else env
        raw_price = source.get(ENV_UNIT_PRICE)
        if raw_price is None or not raw_price.strip():
            raise MeteringConfigError(
                f"未配置单行单价（{ENV_UNIT_PRICE}）：计量单价必须来自配置，禁止硬编码"
            )
        unit_price = _decimal(ENV_UNIT_PRICE, raw_price)
        if unit_price <= 0:
            raise MeteringConfigError(f"{ENV_UNIT_PRICE} 必须大于 0，实际为 {raw_price!r}")

        # 0008 起批次快照的 currency 是 char(3) 且带 ISO 形状 CHECK；这里提前拒绝
        # 'USDT' / 'cny' 这类值，避免落库时才失败（也避免「账单币种」与「快照币种」不一致）。
        currency = (source.get(ENV_CURRENCY) or "CNY").strip().upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise MeteringConfigError(
                f"{ENV_CURRENCY} 必须是三位大写 ISO 币种代码，实际为 {currency!r}"
            )

        unit_label = (source.get(ENV_UNIT_LABEL) or DEFAULT_UNIT_LABEL).strip()

        raw_cap = source.get(ENV_CAP_AMOUNT)
        cap_amount = (
            _decimal(ENV_CAP_AMOUNT, raw_cap)
            if raw_cap is not None and raw_cap.strip()
            else None
        )
        if cap_amount is not None and cap_amount < 0:
            raise MeteringConfigError(f"{ENV_CAP_AMOUNT} 不得为负")

        raw_prior = source.get(ENV_PRIOR_AMOUNT)
        prior_amount = (
            _decimal(ENV_PRIOR_AMOUNT, raw_prior)
            if raw_prior is not None and raw_prior.strip()
            else Decimal(0)
        )
        if prior_amount < 0:
            raise MeteringConfigError(f"{ENV_PRIOR_AMOUNT} 不得为负")

        return cls(
            unit_price=unit_price,
            currency=currency,
            unit_label=unit_label,
            cap_amount=cap_amount,
            prior_amount=prior_amount,
        )


def _decimal(name: str, raw: str) -> Decimal:
    try:
        return Decimal(raw.strip())
    except (InvalidOperation, ValueError) as exc:
        raise MeteringConfigError(f"{name} 必须是十进制数，实际为 {raw!r}") from exc


def build_statement(
    snapshot: BatchSnapshot,
    policy: MeteringPolicy,
    *,
    now: datetime | None = None,
) -> MeteringStatementOut:
    """把持久化快照投影成计量表；不改变任何状态，也不产生外部写入。"""

    all_lines = list(snapshot.lines)
    supplier_side = [line for line in all_lines if line.supplier_ref]

    # 计数与逐行 `billing_bucket` 必须同源：只在这里调用一次分类函数，别处不重算。
    by_bucket: dict[BillingBucket, list[SnapshotLine]] = {
        bucket: [] for bucket in BillingBucket
    }
    for line in supplier_side:
        by_bucket[
            classify_billing_bucket(status=line.status, needs_manual=line.needs_manual)
        ].append(line)

    expected = (
        int(snapshot.metrics.get("supplier_lines", len(supplier_side)))
        if snapshot.metrics is not None
        else len(supplier_side)
    )
    auto_count = len(by_bucket[BillingBucket.BILLABLE]) + len(
        by_bucket[BillingBucket.AUTO_RELEASED]
    )
    hold_count = len(by_bucket[BillingBucket.DISPUTE_HOLD])
    other_lines = by_bucket[BillingBucket.OTHER_UNBILLABLE]
    other_count = len(other_lines)

    # 已确认错放行 / 客户原因：当前无数据源，按 0 扣减并把口径标成「上界」。
    confirmed_misrelease: int | None = None
    customer_reason: int | None = None
    billable_count = len(by_bucket[BillingBucket.BILLABLE])

    # 显式 ROUND_HALF_UP：Decimal 默认 HALF_EVEN 会在 x.xx5 少 1 分（0008 的取整口径）。
    # 批次级只保留 4 位、不取整到分；账期汇总层才做唯一一次取整。
    unit_price = policy.unit_price.quantize(MONEY_EXP, rounding=ROUND_HALF_UP)
    period_amount = (Decimal(billable_count) * unit_price).quantize(
        MONEY_EXP, rounding=ROUND_HALF_UP
    )
    prior_amount = policy.prior_amount.quantize(MONEY_EXP, rounding=ROUND_HALF_UP)
    cumulative = (prior_amount + period_amount).quantize(
        MONEY_EXP, rounding=ROUND_HALF_UP
    )
    cap_amount = (
        policy.cap_amount.quantize(MONEY_EXP, rounding=ROUND_HALF_UP)
        if policy.cap_amount is not None
        else None
    )
    cap_remaining = (
        (cap_amount - cumulative).quantize(MONEY_EXP, rounding=ROUND_HALF_UP)
        if cap_amount is not None
        else None
    )
    cap_exceeded = cap_remaining is not None and cap_remaining < 0

    lines = [_line_out(line) for line in all_lines]

    data_gaps = _data_gaps(lines)
    if expected != len(supplier_side):
        data_gaps.append(
            MeteringDataGapOut(
                field="expected_lines",
                reason=(
                    f"metrics.supplier_lines={expected} 与可列明细行数 "
                    f"{len(supplier_side)} 不一致；分类计数以明细行为准"
                ),
                owner_role="对账引擎负责人",
                action="核对引擎 metrics.supplier_lines 与逐行结果的产出差异并修复",
            )
        )

    other_reasons: dict[str, int] = {}
    for line in other_lines:
        code = OTHER_EXCLUSION_REASONS_BY_VALUE.get(
            line.status, f"other_{line.status}"
        )
        other_reasons[code] = other_reasons.get(code, 0) + 1

    calculation = [
        f"应处理行数 = 供应商对账单行数 = {expected}（来源：对账引擎 metrics.supplier_lines）",
        f"自动放行行数 = 引擎状态「已匹配」且无需人工 = {auto_count}",
        f"争议挂起行数 = 引擎标记需人工（待人工/匹配歧义/候选待人工）= {hold_count}",
        f"其他不计费行数 = 供应商侧未匹配/重复计费/不可解析 = {other_count}",
        (
            "计费桶 = billable "
            f"{len(by_bucket[BillingBucket.BILLABLE])} + auto_released "
            f"{len(by_bucket[BillingBucket.AUTO_RELEASED])} + dispute_hold "
            f"{hold_count} + other_unbillable {other_count} = 明细行数 "
            f"{len(supplier_side)}（逐行 billing_bucket 与计数同源）"
        ),
        (
            "已确认错放行行数 = 无数据源（未接入，按 0 扣减）；"
            "客户原因不计费行数 = 无数据源（未接入，按 0 扣减）"
        ),
        (
            f"计费行数（billable_lines）= 自动放行 {auto_count} − 已确认错放行 0 "
            f"= {billable_count}（上界：确认数据接入后只会下调）"
        ),
        (
            f"本期金额 = 计费行数 {billable_count} × 单行单价 {unit_price} "
            f"= {period_amount} {policy.currency}"
        ),
        f"累计金额 = 已计金额 {prior_amount} + 本期金额 {period_amount} = {cumulative}",
        (
            f"封顶余额 = 封顶金额 {cap_amount} − 累计金额 {cumulative} = {cap_remaining}"
            if cap_amount is not None
            else f"未配置封顶金额（{ENV_CAP_AMOUNT}），封顶余额不可核对"
        ),
    ]

    return MeteringStatementOut(
        batch_id=snapshot.batch_id,
        batch_status=snapshot.status,
        conclusive=snapshot.conclusive,
        period=snapshot.period,
        supplier_scope=list(snapshot.suppliers),
        expected_lines=expected,
        auto_released_lines=auto_count,
        human_reviewed_lines=None,
        confirmed_misrelease_lines=confirmed_misrelease,
        customer_reason_lines=customer_reason,
        billable_lines=billable_count,
        billable_basis=(
            "billable_lines = auto_released_lines − confirmed_misrelease_lines "
            "− customer_reason_lines；后两项当前无数据源，按 0 扣减，故为可计费上界"
        ),
        excluded_lines=MeteringExcludedOut(
            total=hold_count + other_count,
            misrelease=None,
            dispute_hold=hold_count,
            customer_reason=None,
            other_unbillable=other_count,
            other_reasons=other_reasons,
        ),
        unit_price=f"{unit_price:.4f}",
        # 封闭枚举，不使用自由字符串：旧快照/旧路径一律视为 env 回落。
        unit_price_source=(
            snapshot.unit_price_source or UnitPriceSource.ENV_FALLBACK
        ).value,
        currency=policy.currency,
        period_amount=f"{period_amount:.4f}",
        prior_cumulative_amount=f"{prior_amount:.4f}",
        cumulative_amount=f"{cumulative:.4f}",
        cap_amount=None if cap_amount is None else f"{cap_amount:.4f}",
        cap_remaining=None if cap_remaining is None else f"{cap_remaining:.4f}",
        cap_exceeded=cap_exceeded,
        line_accounting=(
            f"应处理 {expected} = 自动放行 {auto_count} + 争议挂起 {hold_count} "
            f"+ 其他不计费 {other_count}（明细行合计 {auto_count + hold_count + other_count}）"
        ),
        calculation=calculation,
        data_gaps=data_gaps,
        computed_at=now or datetime.now(UTC),
        line_count=len(lines),
        lines=lines,
    )


def _data_gaps(lines: list[MeteringLineOut]) -> list[MeteringDataGapOut]:
    gaps = [
        MeteringDataGapOut(
            field="confirmed_misrelease_lines",
            reason="无「已确认错放行」模型/事件，无法扣减计费行；当前按 0 扣减",
            owner_role="对账运营",
            action="人工复核结案后回填已确认错放行的行号与凭证，再据此扣减计费行",
        ),
        MeteringDataGapOut(
            field="customer_reason_lines",
            reason="无「客户原因不计费」标记，无法从计费行中剔除",
            owner_role="客户成功/商务",
            action="回填客户原因不计费的对账行、原因码与确认人，供计量剔除",
        ),
        MeteringDataGapOut(
            field="human_reviewed_lines/human_modified",
            reason="无 review_event 或人工修改留痕，无法区分「引擎放行」与「人工改后放行」",
            owner_role="复核人",
            action="提供行级 review 事件与人工修改前后值（含修改人/时间）",
        ),
        MeteringDataGapOut(
            field="ticket_no",
            reason="对账结果与工单无关联字段，行级工单号不可得",
            owner_role="对账运营",
            action="提供对账行与工单的关联关系（工单号+关联时间）",
        ),
        MeteringDataGapOut(
            field="before_ref/after_ref",
            reason="对账结果与审计影像无关联，行级前像/后像不可得",
            owner_role="审计影像负责人",
            action="提供行级审计影像引用（before_ref/after_ref）与对应 hash",
        ),
    ]
    if any(line.supplier_code is None for line in lines if line.side == "supplier"):
        gaps.append(
            MeteringDataGapOut(
                field="supplier_code",
                reason="行级供应商编码未随引擎结果保留，仅差异行可回填",
                owner_role="主数据管理员",
                action="回填供应商主数据编码并在对账输出中保留到行级",
            )
        )
    return gaps


def _line_out(line: SnapshotLine) -> MeteringLineOut:
    if line.auto_released:
        excluded_reason = ""
    elif line.needs_manual:
        excluded_reason = "dispute_hold"
    else:
        excluded_reason = OTHER_EXCLUSION_REASONS_BY_VALUE.get(
            line.status, f"other_{line.status}"
        )

    evidence = [
        EvidenceOut(
            system=str(item["system"]),
            doc_no=str(item["doc_no"]),
            field=str(item["field"]),
            coord=item.get("coord"),
        )
        for item in line.evidence
    ]

    return MeteringLineOut(
        line_no=line.line_no,
        side=line.side,
        supplier_code=line.supplier_code,
        our_ref=line.our_ref,
        supplier_ref=line.supplier_ref,
        status=line.status,
        auto_released=line.auto_released,
        needs_manual=line.needs_manual,
        billing_bucket=classify_billing_bucket(
            status=line.status, needs_manual=line.needs_manual
        ),
        human_modified=None,
        ticket_no=None,
        before_ref=None,
        after_ref=None,
        excluded_reason=excluded_reason,
        diff_types=list(line.diff_types),
        owner=line.owner,
        confidence=line.confidence or "",
        source_refs=list(line.source_refs),
        evidence=evidence,
    )


def statement_csv(statement: MeteringStatementOut) -> tuple[bytes, str]:
    """逐行明细 CSV（UTF-8 BOM）；列与 ``METERING_LINE_COLUMNS`` 完全一致。"""

    rows = [",".join(METERING_LINE_COLUMNS)]
    for line in statement.lines:
        rows.append(
            ",".join(
                _cell(value)
                for value in (
                    line.line_no,
                    line.side,
                    line.supplier_code,
                    line.our_ref,
                    line.supplier_ref,
                    line.status,
                    line.billing_bucket.value,
                    _bool_text(line.auto_released),
                    _bool_text(line.needs_manual),
                    _bool_text(line.human_modified),
                    line.ticket_no,
                    line.before_ref,
                    line.after_ref,
                    line.excluded_reason,
                    "|".join(line.diff_types),
                    line.owner,
                    line.confidence,
                    "|".join(line.source_refs),
                    "|".join(
                        f"{item.system}:{item.doc_no}#{item.field}"
                        for item in line.evidence
                    ),
                )
            )
        )
    content = ("\n".join(rows) + "\n").encode("utf-8-sig")
    return content, hashlib.sha256(content).hexdigest()


def _num(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _bool_text(value: bool | None) -> str:
    if value is None:
        return ""
    return "true" if value else "false"


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(char in text for char in ',"\n\r'):
        return '"' + text.replace('"', '""') + '"'
    return text


__all__ = [
    "ENV_CAP_AMOUNT",
    "ENV_CURRENCY",
    "ENV_PRIOR_AMOUNT",
    "ENV_UNIT_LABEL",
    "ENV_UNIT_PRICE",
    "METERING_LINE_COLUMNS",
    "MeteringConfigError",
    "MeteringPolicy",
    "build_statement",
    "statement_csv",
]
