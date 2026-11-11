"""``ticket_step.audit_image_id`` ↔ ``erp.audit_image`` 的唯一关联口径。

读路径（``/v1/audit/images``、``/v1/audit/chain``、CSV 导出）与巡检
（``B-AUDIT-ORPHAN``，``apps/api/audit_integrity.py``）必须引用同一个谓词
``STEP_IMAGE_PREDICATE``：关联只在这里定义一次，孤儿定义与证据列表不会分叉。

读路径的语义：

- 影像行只来自 ``erp.audit_image``（不读进程内台账、不读 ``ticket_step`` 的副本）；
- 一个工单的影像集合 = 被该工单的步骤通过 ``audit_image_id`` 引用到的影像；
- 巡检的孤儿 = ``audit_image_id`` 非空、但同一谓词在 ``audit_image`` 里找不到行。

方向差异（有意保留，不在读路径里补假数据）：``audit_image`` 存在、但没有任何
``ticket_step`` 指向它的行，不属于「步骤有证据却缺证据行」，读路径不会列出它；
这类「反向孤儿」不是 ``B-AUDIT-ORPHAN`` 的口径，若将来要纳入需单独开卡。
"""

from __future__ import annotations

from sqlalchemy import TextClause, text

#: 步骤指针 ↔ 影像行的唯一关联谓词（``s`` = ticket_step，``i`` = audit_image）。
STEP_IMAGE_PREDICATE = "i.id = s.audit_image_id"

ORPHAN_SQL: TextClause = text(
    f"""
    WITH orphan AS (
        SELECT s.id AS step_id, s.ticket_id, s.audit_image_id
          FROM erp.ticket_step AS s
         WHERE s.audit_image_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM erp.audit_image AS i WHERE {STEP_IMAGE_PREDICATE}
           )
    )
    SELECT (SELECT count(*) FROM orphan) AS orphan_count,
           (SELECT json_agg(sample)::text
              FROM (SELECT step_id, ticket_id, audit_image_id
                      FROM orphan
                     ORDER BY ticket_id, step_id
                     LIMIT :sample_limit) AS sample) AS samples
    """
)

_IMAGE_COLUMNS = """
    i.id, i.tenant_id, i.ticket_id, i.step_id, i.system, i.object_type,
    i.object_id, i.before_ref, i.after_ref, i.field_diff, i.diff_missing,
    i.prev_hash, i.hash, i.created_at
"""


def _image_where(
    *,
    by_ticket: bool,
    by_step: bool,
    by_system: bool,
    by_object: bool,
    by_since: bool,
    by_until: bool,
) -> str:
    """按可选条件拼 WHERE；条件片段是常量，参数一律走绑定。"""

    step_filter = "AND s.id = :step_id" if by_step else ""
    clauses = [
        "i.tenant_id = :tenant_id",
        f"""
        EXISTS (
            SELECT 1 FROM erp.ticket_step AS s
             WHERE s.ticket_id = i.ticket_id
               AND {STEP_IMAGE_PREDICATE}
               {step_filter}
        )
        """,
    ]
    if by_ticket:
        clauses.append("i.ticket_id = :ticket_id")
    if by_system:
        clauses.append("i.system = :system")
    if by_object:
        clauses.append("i.object_id = :object_id")
    if by_since:
        clauses.append("i.created_at >= :since")
    if by_until:
        clauses.append("i.created_at <= :until")
    return "\n          AND ".join(clauses)


def images_sql(
    *,
    by_ticket: bool,
    by_step: bool,
    by_system: bool,
    by_object: bool,
    by_since: bool,
    by_until: bool,
) -> TextClause:
    """按可选条件生成工单影像查询；条件片段是常量，参数一律走绑定。"""

    where = _image_where(
        by_ticket=by_ticket,
        by_step=by_step,
        by_system=by_system,
        by_object=by_object,
        by_since=by_since,
        by_until=by_until,
    )
    return text(
        f"""
        SELECT {_IMAGE_COLUMNS}
          FROM erp.audit_image AS i
         WHERE {where}
         ORDER BY i.chain_seq
         OFFSET :offset LIMIT :limit
        """
    )


def images_count_sql(
    *,
    by_ticket: bool,
    by_step: bool,
    by_system: bool,
    by_object: bool,
    by_since: bool,
    by_until: bool,
) -> TextClause:
    """与 :func:`images_sql` 共用同一 WHERE，只取行数。

    工单详情只展示张数，不应为了一个整数把全部 ``field_diff`` 物化一遍；
    计数与列表必须同源，否则「显示 N 张」和「点开只有 M 张」会再次分叉。
    """

    where = _image_where(
        by_ticket=by_ticket,
        by_step=by_step,
        by_system=by_system,
        by_object=by_object,
        by_since=by_since,
        by_until=by_until,
    )
    return text(
        f"""
        SELECT count(*)
          FROM erp.audit_image AS i
         WHERE {where}
        """
    )


__all__ = ["ORPHAN_SQL", "STEP_IMAGE_PREDICATE", "images_count_sql", "images_sql"]
