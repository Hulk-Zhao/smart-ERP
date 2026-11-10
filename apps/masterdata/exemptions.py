"""阻断豁免：有期限、可审计、不可覆盖结构性问题（只读）。

豁免不是放宽规则，而是对「已核实无误」的个别主体 + 个别规则开一个有到期日的口子：

- 必有所提人、批准人、依据（客户书面确认）、原因、生效日与到期日；
- 客户确认函编号 ``basis.ref`` 全局唯一，不得跨豁免复用；确认函自身有有效期
  （``basis.valid_from``/``basis.valid_to``），豁免期越界即拒绝；
- 批准人不得是计费受益角色（商务/销售/客户成功），避免「豁免越多、计费越多」的利益冲突；
- 到期自动失效，无需人工清理；
- 税号重复、编码重复、缺基本单位等结构性问题不允许豁免（见
  ``NON_EXEMPTIBLE_RULES``），避免把「修数据」变成「盖豁免章」；
- 单批豁免影响行占比超过 15% 时必须二人复核；整批豁免一律拒绝；
- 同一主体 + 同一规则累计豁免超过 3 次时要求升级复核；
- 豁免只作用于本次报告：把命中的阻断差异降为提示，并留下 ``AppliedExemption``
  审计记录；不修改快照、不写回 ERP。

豁免配置读不到或格式非法时，调用方不得静默跳过；CLI 直接报错退出。对账引擎若
自行接入，应按「不应用任何豁免」处理（阻断照常生效），这是比放宽更安全的一侧。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from .impact import ReconLine, resolve_subject_keys
from .models import (
    AppliedExemption,
    Evidence,
    InspectionReport,
    MasterDataSnapshot,
    ObjectType,
    Severity,
    format_ratio,
)
from .rules import RULES_BY_ID

SCHEMA_VERSION = "2.0"
KIND = "masterdata_exemptions"
_SUBJECT_TYPES = (ObjectType.SUPPLIER, ObjectType.CUSTOMER, ObjectType.ITEM)
_WILDCARD_SUBJECTS = {"*", "ALL", "全部", "所有"}

# 角色分离：计费受益角色不能批准豁免，防止「多豁免 = 多计费」的利益冲突。
SUBMITTER_ROLES = ("对账运营",)
APPROVER_ROLES = ("财务", "内控")
BILLING_ROLES = ("商务", "销售", "客户成功")
CUSTOMER_CONFIRMATION_TYPE = "客户书面确认"

# 单批豁免影响行占比超过该阈值时，必须二人复核并出告警。
EXEMPTION_LINE_RATIO_LIMIT = Decimal("0.15")
# 同一主体 + 同一规则累计豁免次数超过该值时，要求升级复核。
MAX_CUMULATIVE_EXEMPTIONS = 3

# 结构性问题：必须修数据，不允许用豁免绕过。值为「为什么不能豁免」。
NON_EXEMPTIBLE_RULES: dict[str, str] = {
    "DUP-001": "同一税号挂多个供应商编码是主体唯一性问题，豁免会让重复付款继续发生",
    "DUP-003": "物料重复编码会把库存与对账数量拆成两半，只能合并",
    "CODE-001": "形近编码是录入/串号的根源，只能改编码或建立显式映射",
    "MISS-003": "缺基本单位无法换算，豁免等于放弃金额校验",
    "UOM-001": "同一单位对存在多套比例，必须确认保留哪一条",
    "UOM-002": "正反向换算乘积不等于 1，是算术矛盾，不存在合理解释",
    "UOM-005": "缺闭合换算路径时该行无法换算成金额",
    "CUR-001": "缺汇率就无法折算本位币金额，豁免只能得到错误金额",
    "CUR-002": "单据汇率与账套汇率不一致会算出两套金额",
    "TAX-001": "引用无效税码会同时影响开票与申报，必须补目录或修正引用",
    "TAX-002": "税率与申报口径不符必须由税务确认后修正",
    "CUST-001": "同一税号挂多个客户编码是主体唯一性问题",
    "LEDGER-001": "单据记错账套必须冲销/重记，豁免会让金额留在错误报表主体",
}


class ExemptionInvalid(ValueError):
    """豁免配置不可读或不符合约定；调用方不得当作「无豁免」静默继续。"""


class ExemptionPolicyViolation(ValueError):
    """豁免通过格式校验，但违反角色分离、整批豁免、复核次数等硬性策略。"""


@dataclass(frozen=True)
class Exemption:
    """一条有期限的豁免：主体 + 规则 + 提单/批准/复核 + 到期日。"""

    exemption_id: str
    subject_type: ObjectType
    subject_code: str
    rule_id: str
    submitted_by: str
    submitted_role: str
    submitted_at: date
    approved_by: str
    approved_role: str
    basis_type: str
    basis_ref: str
    basis_valid_from: date
    basis_valid_to: date
    reason: str
    granted_at: date
    expires_at: date
    second_reviewer: str = ""
    second_reviewed_role: str = ""
    escalated_review_by: str = ""
    cumulative_count: int = 1

    def is_active(self, at: date) -> bool:
        return self.granted_at <= at <= self.expires_at

    def is_expired(self, at: date) -> bool:
        return at > self.expires_at

    def is_pending(self, at: date) -> bool:
        return at < self.granted_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "exemption_id": self.exemption_id,
            "subject_type": self.subject_type.value,
            "subject_code": self.subject_code,
            "rule_id": self.rule_id,
            "submitted_by": self.submitted_by,
            "submitted_role": self.submitted_role,
            "submitted_at": self.submitted_at.isoformat(),
            "approved_by": self.approved_by,
            "approved_role": self.approved_role,
            "basis_type": self.basis_type,
            "basis_ref": self.basis_ref,
            "basis_valid_from": self.basis_valid_from.isoformat(),
            "basis_valid_to": self.basis_valid_to.isoformat(),
            "reason": self.reason,
            "granted_at": self.granted_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "second_reviewer": self.second_reviewer,
            "second_reviewed_role": self.second_reviewed_role,
            "escalated_review_by": self.escalated_review_by,
            "cumulative_count": self.cumulative_count,
        }


@dataclass(frozen=True)
class ExemptionRegistry:
    source: str
    entries: tuple[Exemption, ...]

    def active(self, at: date) -> tuple[Exemption, ...]:
        return tuple(item for item in self.entries if item.is_active(at))

    def expired(self, at: date) -> tuple[Exemption, ...]:
        return tuple(item for item in self.entries if item.is_expired(at))

    def pending(self, at: date) -> tuple[Exemption, ...]:
        return tuple(item for item in self.entries if item.is_pending(at))

    def to_dict(self, at: date | None = None) -> dict[str, Any]:
        current = at or date.today()
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "source": self.source,
            "as_of": current.isoformat(),
            "entries": [item.to_dict() for item in self.entries],
            "active_ids": [item.exemption_id for item in self.active(current)],
            "expired_ids": [item.exemption_id for item in self.expired(current)],
            "pending_ids": [item.exemption_id for item in self.pending(current)],
            "non_exemptible_rules": dict(NON_EXEMPTIBLE_RULES),
            "policy": {
                "submitter_roles": list(SUBMITTER_ROLES),
                "approver_roles": list(APPROVER_ROLES),
                "billing_roles": list(BILLING_ROLES),
                "customer_confirmation_type": CUSTOMER_CONFIRMATION_TYPE,
                "line_ratio_limit": str(EXEMPTION_LINE_RATIO_LIMIT),
                "max_cumulative_count": MAX_CUMULATIVE_EXEMPTIONS,
                "blanket_exemption": "rejected",
            },
        }


def parse_exemptions(raw: Any) -> ExemptionRegistry:
    """解析并校验豁免配置；任何问题都抛 ``ExemptionInvalid``。"""
    if not isinstance(raw, Mapping):
        raise ExemptionInvalid("豁免配置必须是 JSON 对象")
    if raw.get("schema_version") != SCHEMA_VERSION:
        found = raw.get("schema_version")
        if found == "1.0":
            raise ExemptionInvalid(
                "检测到 schema 1.0：缺少 submitted_by / submitted_role / "
                "submitted_at、approved_role 与类型化 basis（必须为客户书面确认）。"
                "请用 `python -m apps.masterdata --migrate-exemptions old.json "
                "--roles roles.json --out new.json` 转换，或参考 EXEMPTIONS.md §迁移；"
                "角色与客户确认必须手工补齐，不能自动猜。"
            )
        raise ExemptionInvalid(
            "豁免配置 schema_version 不兼容："
            f"期望 {SCHEMA_VERSION}，收到 {found!r}"
        )
    if raw.get("kind") != KIND:
        raise ExemptionInvalid(
            f"豁免配置 kind 不匹配：期望 {KIND}，收到 {raw.get('kind')!r}"
        )
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list):
        raise ExemptionInvalid("豁免配置缺少 entries 数组")

    entries: list[Exemption] = []
    seen_ids: set[str] = set()
    seen_refs: dict[str, str] = {}
    for index, item in enumerate(entries_raw):
        if not isinstance(item, Mapping):
            raise ExemptionInvalid(f"entries[{index}] 不是对象")
        needed = (
            "exemption_id",
            "subject_type",
            "subject_code",
            "rule_id",
            "submitted_by",
            "submitted_role",
            "submitted_at",
            "approved_by",
            "approved_role",
            "reason",
            "granted_at",
            "expires_at",
        )
        missing = [key for key in needed if not item.get(key)]
        if missing:
            raise ExemptionInvalid(
                f"entries[{index}] 缺少字段: {', '.join(missing)}"
            )
        basis = item.get("basis")
        if not isinstance(basis, Mapping) or not basis.get("type") or not basis.get(
            "ref"
        ):
            raise ExemptionInvalid(
                f"entries[{index}] 缺少 basis.type / basis.ref（客户书面确认）"
            )
        try:
            subject_type = ObjectType(item["subject_type"])
        except ValueError as exc:
            raise ExemptionInvalid(
                f"entries[{index}] 主体类型非法: {exc}"
            ) from exc
        if subject_type not in _SUBJECT_TYPES:
            raise ExemptionInvalid(
                f"entries[{index}] 主体类型必须是供应商/客户/物料"
            )
        subject_code = str(item["subject_code"]).strip()
        if not subject_code or subject_code.upper() in _WILDCARD_SUBJECTS:
            raise ExemptionInvalid(
                f"entries[{index}] 不允许整批/通配主体编码 {subject_code!r}；"
                "整批豁免一律拒绝"
            )
        rule_id = str(item["rule_id"])
        if rule_id not in RULES_BY_ID:
            raise ExemptionInvalid(
                f"entries[{index}] 规则 {rule_id} 不在规则目录中"
            )
        if rule_id in NON_EXEMPTIBLE_RULES:
            raise ExemptionInvalid(
                f"entries[{index}] 规则 {rule_id} 不允许豁免："
                f"{NON_EXEMPTIBLE_RULES[rule_id]}"
            )
        _check_roles(index, item, basis)
        exemption_id = str(item["exemption_id"])
        if exemption_id in seen_ids:
            raise ExemptionInvalid(f"豁免编号重复: {exemption_id}")
        seen_ids.add(exemption_id)
        basis_ref = str(basis["ref"]).strip()
        if not basis.get("valid_from") or not basis.get("valid_to"):
            raise ExemptionInvalid(
                f"entries[{index}] 缺少 basis.valid_from / basis.valid_to"
                "（客户确认函有效期）"
            )
        if basis_ref in seen_refs:
            raise ExemptionInvalid(
                f"客户确认函编号 {basis_ref} 重复出现在 "
                f"{seen_refs[basis_ref]} 与 {exemption_id}："
                "同一确认函不得跨豁免复用"
            )
        seen_refs[basis_ref] = exemption_id
        try:
            submitted_at = date.fromisoformat(str(item["submitted_at"]))
            granted_at = date.fromisoformat(str(item["granted_at"]))
            expires_at = date.fromisoformat(str(item["expires_at"]))
            basis_valid_from = date.fromisoformat(str(basis["valid_from"]))
            basis_valid_to = date.fromisoformat(str(basis["valid_to"]))
        except ValueError as exc:
            raise ExemptionInvalid(
                f"entries[{index}] 日期必须是 YYYY-MM-DD: {exc}"
            ) from exc
        if expires_at < granted_at:
            raise ExemptionInvalid(
                f"entries[{index}] 到期日 {expires_at} 早于生效日 {granted_at}"
            )
        if submitted_at > granted_at:
            raise ExemptionInvalid(
                f"entries[{index}] 提单日 {submitted_at} 晚于生效日 {granted_at}"
            )
        if basis_valid_to < basis_valid_from:
            raise ExemptionInvalid(
                f"entries[{index}] 客户确认函有效期非法：valid_to "
                f"{basis_valid_to} 早于 valid_from {basis_valid_from}"
            )
        if granted_at < basis_valid_from or expires_at > basis_valid_to:
            raise ExemptionInvalid(
                f"entries[{index}] 豁免期 {granted_at}~{expires_at} 超出客户确认函"
                f"有效期 {basis_valid_from}~{basis_valid_to}；越界豁免一律拒绝"
            )
        second_reviewer = str(item.get("second_reviewer") or "")
        second_reviewed_role = str(item.get("second_reviewed_role") or "")
        if second_reviewer:
            if second_reviewed_role not in APPROVER_ROLES:
                raise ExemptionInvalid(
                    f"entries[{index}] 二次复核角色必须是财务/内控"
                )
            if second_reviewer in {str(item["approved_by"]), str(item["submitted_by"])}:
                raise ExemptionInvalid(
                    f"entries[{index}] 二次复核人必须独立于提单人与批准人"
                )
        entries.append(
            Exemption(
                exemption_id=exemption_id,
                subject_type=subject_type,
                subject_code=subject_code,
                rule_id=rule_id,
                submitted_by=str(item["submitted_by"]),
                submitted_role=str(item["submitted_role"]),
                submitted_at=submitted_at,
                approved_by=str(item["approved_by"]),
                approved_role=str(item["approved_role"]),
                basis_type=str(basis["type"]),
                basis_ref=basis_ref,
                basis_valid_from=basis_valid_from,
                basis_valid_to=basis_valid_to,
                reason=str(item["reason"]),
                granted_at=granted_at,
                expires_at=expires_at,
                second_reviewer=second_reviewer,
                second_reviewed_role=second_reviewed_role,
                escalated_review_by=str(item.get("escalated_review_by") or ""),
            )
        )

    _reject_overlaps(entries)
    counted = _assign_cumulative_counts(entries)
    _require_escalation(counted)
    counted.sort(key=lambda item: item.exemption_id)
    return ExemptionRegistry(
        source=str(raw.get("source") or ""),
        entries=tuple(counted),
    )


def _check_roles(index: int, item: Mapping[str, Any], basis: Mapping[str, Any]) -> None:
    submitted_role = str(item["submitted_role"]).strip()
    approved_role = str(item["approved_role"]).strip()
    if approved_role in BILLING_ROLES:
        raise ExemptionInvalid(
            f"entries[{index}] 批准人不能是计费受益角色（{approved_role}）；"
            "豁免会增加自动放行与计费行，须由财务/内控复核"
        )
    if approved_role not in APPROVER_ROLES:
        raise ExemptionInvalid(
            f"entries[{index}] 批准角色必须是财务/内控，收到 {approved_role!r}"
        )
    if submitted_role not in SUBMITTER_ROLES:
        raise ExemptionInvalid(
            f"entries[{index}] 提单角色必须是{'/'.join(SUBMITTER_ROLES)}，"
            f"收到 {submitted_role!r}"
        )
    if str(item["submitted_by"]) == str(item["approved_by"]):
        raise ExemptionInvalid(
            f"entries[{index}] 提单人与批准人不能是同一人（角色分离）"
        )
    if str(basis.get("type")) != CUSTOMER_CONFIRMATION_TYPE:
        raise ExemptionInvalid(
            f"entries[{index}] 依据类型必须是 {CUSTOMER_CONFIRMATION_TYPE}，"
            f"收到 {basis.get('type')!r}"
        )


def _reject_overlaps(entries: list[Exemption]) -> None:
    """同一主体 + 同一规则不允许同时存在两段重叠豁免，避免「以新盖旧」失控。"""
    grouped: dict[tuple[ObjectType, str, str], list[Exemption]] = {}
    for entry in entries:
        grouped.setdefault(
            (entry.subject_type, entry.subject_code, entry.rule_id), []
        ).append(entry)
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: item.granted_at)
        for previous, current in pairwise(ordered):
            if current.granted_at <= previous.expires_at:
                raise ExemptionInvalid(
                    "同一主体与规则的豁免有效期重叠："
                    f"{previous.exemption_id} 与 {current.exemption_id}"
                )


def _assign_cumulative_counts(entries: list[Exemption]) -> list[Exemption]:
    """按主体 + 规则累计历史豁免次数（含已过期），写回每条记录。"""
    grouped: dict[tuple[ObjectType, str, str], list[Exemption]] = {}
    for entry in entries:
        grouped.setdefault(
            (entry.subject_type, entry.subject_code, entry.rule_id), []
        ).append(entry)
    counted: dict[str, Exemption] = {}
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: item.granted_at)
        for index, entry in enumerate(ordered, 1):
            counted[entry.exemption_id] = replace(entry, cumulative_count=index)
    return [counted[entry.exemption_id] for entry in entries]


def _require_escalation(entries: Sequence[Exemption]) -> None:
    for entry in entries:
        if entry.cumulative_count <= MAX_CUMULATIVE_EXEMPTIONS:
            continue
        if not entry.escalated_review_by:
            raise ExemptionInvalid(
                f"豁免 {entry.exemption_id} 是同一主体同规则第 "
                f"{entry.cumulative_count} 次豁免，超过 "
                f"{MAX_CUMULATIVE_EXEMPTIONS} 次必须填写 escalated_review_by"
            )
        if entry.escalated_review_by in {
            entry.approved_by,
            entry.submitted_by,
            entry.second_reviewer,
        }:
            raise ExemptionInvalid(
                f"豁免 {entry.exemption_id} 的升级复核人必须独立于提单/批准/二次复核人"
            )


def migrate_v1_to_v2(
    raw: Any, mappings: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """把 schema 1.0 豁免手工迁移到 2.0。

    ``mappings`` 必须按 ``exemption_id`` 提供 2.0 新增字段（submitted_by /
    submitted_role / submitted_at / approved_role / basis_ref /
    basis_valid_from / basis_valid_to），函数**不会**从旧字段猜测角色或把旧依据
    当成客户书面确认。迁移结果会先过 ``parse_exemptions`` 校验，不合法直接抛
    ``ExemptionInvalid``。
    """
    if not isinstance(raw, Mapping):
        raise ExemptionInvalid("待迁移的豁免配置必须是 JSON 对象")
    if raw.get("schema_version") != "1.0":
        raise ExemptionInvalid(
            "migrate_v1_to_v2 只接受 schema_version=1.0 的配置，"
            f"收到 {raw.get('schema_version')!r}"
        )
    if not isinstance(mappings, Mapping):
        raise ExemptionInvalid("迁移映射必须是 {exemption_id: {...}} 对象")
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list):
        raise ExemptionInvalid("待迁移配置缺少 entries 数组")

    required = (
        "submitted_by",
        "submitted_role",
        "submitted_at",
        "approved_role",
        "basis_ref",
        "basis_valid_from",
        "basis_valid_to",
    )
    migrated: list[dict[str, Any]] = []
    for index, item in enumerate(entries_raw):
        if not isinstance(item, Mapping):
            raise ExemptionInvalid(f"entries[{index}] 不是对象")
        exemption_id = str(item.get("exemption_id") or "")
        if not exemption_id:
            raise ExemptionInvalid(f"entries[{index}] 缺少 exemption_id")
        override = mappings.get(exemption_id)
        if not isinstance(override, Mapping):
            raise ExemptionInvalid(
                f"迁移 {exemption_id} 时必须手工提供角色与客户确认映射；"
                "不能自动猜"
            )
        missing = [key for key in required if not override.get(key)]
        if missing:
            raise ExemptionInvalid(
                f"迁移 {exemption_id} 缺少字段: {', '.join(missing)}"
            )
        migrated.append(
            {
                "exemption_id": exemption_id,
                "subject_type": item.get("subject_type"),
                "subject_code": item.get("subject_code"),
                "rule_id": item.get("rule_id"),
                "submitted_by": override["submitted_by"],
                "submitted_role": override["submitted_role"],
                "submitted_at": override["submitted_at"],
                "approved_by": item.get("approved_by"),
                "approved_role": override["approved_role"],
                "basis": {
                    "type": CUSTOMER_CONFIRMATION_TYPE,
                    "ref": override["basis_ref"],
                    "valid_from": override["basis_valid_from"],
                    "valid_to": override["basis_valid_to"],
                },
                "reason": item.get("reason"),
                "granted_at": item.get("granted_at"),
                "expires_at": item.get("expires_at"),
                "second_reviewer": override.get("second_reviewer", ""),
                "second_reviewed_role": override.get("second_reviewed_role", ""),
                "escalated_review_by": override.get("escalated_review_by", ""),
            }
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "source": raw.get("source", ""),
        "entries": migrated,
    }
    parse_exemptions(result)
    return result


def load_exemptions(path: str | Path) -> ExemptionRegistry:
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExemptionInvalid(f"无法读取豁免配置 {target}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExemptionInvalid(f"豁免配置不是合法 JSON {target}: {exc}") from exc
    registry = parse_exemptions(raw)
    return ExemptionRegistry(source=str(target), entries=registry.entries)


def _in_period_lines(
    report: InspectionReport,
    lines: Sequence[ReconLine],
) -> list[ReconLine]:
    return [
        line
        for line in lines
        if line.period is None or line.period == report.as_of_period
    ]


def _line_keys(line: ReconLine) -> tuple[tuple[ObjectType, str], ...]:
    keys: list[tuple[ObjectType, str]] = []
    if line.supplier_code:
        keys.append((ObjectType.SUPPLIER, line.supplier_code))
    if line.customer_code:
        keys.append((ObjectType.CUSTOMER, line.customer_code))
    if line.item_code:
        keys.append((ObjectType.ITEM, line.item_code))
    return tuple(keys)


def _exemption_covered_lines(
    report: InspectionReport,
    exempted_pairs: set[tuple[str, ObjectType, str, str]],
    lines: Sequence[ReconLine],
    snapshot: MasterDataSnapshot | None,
) -> dict[str, set[str]]:
    """每条豁免实际覆盖到的本期行（只按「主体 + 规则」匹配，不判断其他规则）。"""
    blocked_by_rule: dict[tuple[ObjectType, str, str], set[str]] = {}
    for finding in report.findings:
        if finding.severity is not Severity.BLOCK:
            continue
        for key in resolve_subject_keys(finding, snapshot):
            blocked_by_rule.setdefault(
                (key[0], key[1], finding.rule_id), set()
            )
    covered: dict[str, set[str]] = {}
    for line in lines:
        keys = _line_keys(line)
        for exemption_id, subject_type, subject_code, rule_id in exempted_pairs:
            if (subject_type, subject_code) in keys and (
                subject_type,
                subject_code,
                rule_id,
            ) in blocked_by_rule:
                covered.setdefault(exemption_id, set()).add(line.line_id)
    return covered


def apply_exemptions(
    report: InspectionReport,
    registry: ExemptionRegistry,
    snapshot: MasterDataSnapshot | None = None,
    at: date | None = None,
    lines: Sequence[ReconLine] | None = None,
) -> InspectionReport:
    """把生效豁免命中的阻断差异降为提示，并写入审计记录。

    只有该差异涉及的**全部**主体都有同规则豁免时才降级；任一主体未豁免则保持阻断，
    避免「一个主体被豁免、连带放过另一个主体」。

    传入 ``lines`` 时额外执行两条硬性策略：

    - 整批豁免（豁免覆盖本期全部行）直接抛 ``ExemptionPolicyViolation``；
    - 豁免影响行占比超过 15% 时，每条命中的豁免都必须有 ``second_reviewer``，
      否则拒绝应用。
    """
    current = at or date.today()
    active = {
        (item.subject_type, item.subject_code, item.rule_id): item
        for item in registry.active(current)
    }
    if not active:
        return InspectionReport(
            source=report.source,
            org_id=report.org_id,
            as_of_period=report.as_of_period,
            findings=report.findings,
            rules=report.rules,
            options=report.options,
            filters=report.filters,
            exemptions=(),
            exemption_cap_checked=lines is not None,
            exemption_cap_note="无生效豁免，未触发影响面上限校验",
        )

    applied: dict[str, dict[str, Any]] = {}
    findings = []
    for finding in report.findings:
        if finding.severity is not Severity.BLOCK:
            findings.append(finding)
            continue
        keys = resolve_subject_keys(finding, snapshot)
        matches = [active.get((key[0], key[1], finding.rule_id)) for key in keys]
        if not keys or not all(matches):
            findings.append(finding)
            continue
        distinct = {item.exemption_id: item for item in matches if item is not None}
        evidence: list[Evidence] = []
        for exemption in distinct.values():
            bucket = applied.setdefault(
                exemption.exemption_id,
                {"exemption": exemption, "findings": []},
            )
            bucket["findings"].append(
                f"{finding.object_type.value}:{finding.object_code}"
            )
            evidence.append(
                Evidence(
                    "豁免",
                    (
                        f"豁免 {exemption.exemption_id} 由 {exemption.submitted_by}"
                        f"（{exemption.submitted_role}）提单、"
                        f"{exemption.approved_by}（{exemption.approved_role}）批准，"
                        f"依据 {exemption.basis_type}:{exemption.basis_ref}，"
                        f"到期 {exemption.expires_at.isoformat()}"
                    ),
                    "豁免期内不降级，到期自动恢复阻断",
                    f"{registry.source}:{exemption.exemption_id}",
                )
            )
        findings.append(
            replace(
                finding,
                severity=Severity.WARN,
                evidence=finding.evidence + tuple(evidence),
            )
        )

    applied_records = tuple(
        AppliedExemption(
            exemption_id=value["exemption"].exemption_id,
            subject_type=value["exemption"].subject_type,
            subject_code=value["exemption"].subject_code,
            rule_id=value["exemption"].rule_id,
            submitted_by=value["exemption"].submitted_by,
            submitted_role=value["exemption"].submitted_role,
            submitted_at=value["exemption"].submitted_at.isoformat(),
            approved_by=value["exemption"].approved_by,
            approved_role=value["exemption"].approved_role,
            basis_type=value["exemption"].basis_type,
            basis_ref=value["exemption"].basis_ref,
            basis_valid_from=value["exemption"].basis_valid_from.isoformat(),
            basis_valid_to=value["exemption"].basis_valid_to.isoformat(),
            reason=value["exemption"].reason,
            granted_at=value["exemption"].granted_at.isoformat(),
            expires_at=value["exemption"].expires_at.isoformat(),
            second_reviewer=value["exemption"].second_reviewer,
            second_reviewed_role=value["exemption"].second_reviewed_role,
            escalated_review_by=value["exemption"].escalated_review_by,
            cumulative_count=value["exemption"].cumulative_count,
            findings=tuple(sorted(value["findings"])),
        )
        for _, value in sorted(applied.items())
    )
    result = InspectionReport(
        source=report.source,
        org_id=report.org_id,
        as_of_period=report.as_of_period,
        findings=tuple(findings),
        rules=report.rules,
        options=report.options,
        filters=report.filters,
        exemptions=applied_records,
    )
    if lines is None:
        return replace(
            result,
            exemption_cap_checked=False,
            exemption_cap_note=(
                "未提供行级数据 → 豁免影响面上限未校验"
                "（15% 二次复核与整批豁免判定均未执行）"
            ),
        )
    _enforce_line_policies(report, result, lines, snapshot)
    return replace(
        result,
        exemption_cap_checked=True,
        exemption_cap_note="已校验：整批豁免拒绝 + 15% 二次复核",
    )


def _enforce_line_policies(
    before: InspectionReport,
    after: InspectionReport,
    lines: Sequence[ReconLine],
    snapshot: MasterDataSnapshot | None,
) -> None:
    in_period = _in_period_lines(before, lines)
    if not in_period:
        return
    exempted_pairs = {
        (
            item.exemption_id,
            item.subject_type,
            item.subject_code,
            item.rule_id,
        )
        for item in after.exemptions
    }
    covered = _exemption_covered_lines(before, exempted_pairs, in_period, snapshot)
    covered_lines = set().union(*covered.values()) if covered else set()
    total = len(in_period)
    if len(covered_lines) >= total:
        raise ExemptionPolicyViolation(
            "整批豁免被拒绝：豁免覆盖了本期全部 "
            f"{total} 行，等同于把整批自动放行；请改为逐主体、可核实的个案豁免"
        )
    ratio = Decimal(len(covered_lines)) / Decimal(total)
    if ratio <= EXEMPTION_LINE_RATIO_LIMIT:
        return
    missing = [
        item.exemption_id
        for item in after.exemptions
        if not item.second_reviewer or not item.second_reviewed_role
    ]
    if missing:
        raise ExemptionPolicyViolation(
            "豁免影响行占比 "
            f"{format_ratio(ratio)} 超过上限 {format_ratio(EXEMPTION_LINE_RATIO_LIMIT)}，"
            f"以下豁免缺少二次复核人：{', '.join(sorted(missing))}"
        )
