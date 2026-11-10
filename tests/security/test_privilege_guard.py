"""守卫：``erp_app`` 对审计/证据类表不得持有任何写权限。

这不是「再补一条用例」，而是把纪律换成会红的测试：仓库当前是「默认放开、靠每个
迁移记得 REVOKE」。今晚已经三次证明纪律会漏（``frozenset`` 被顺手删、``expose: 8011``
只删一半、脚本从未入库）。所以这里不枚举某几张表，而是**扫整个 erp schema**：

1. 证据类表（``AUDIT_EVIDENCE_TABLES``）一旦被授予 UPDATE/DELETE/TRUNCATE → 失败；
2. 其它表若要持有写权限，必须在本文件里**逐表登记理由**；未登记的新表出现写权限
   → 失败（不给通配、不给「除白名单外都放过」）；
3. 登记表若已不存在或已无写权限 → 失败（防止豁免变成永久的死条目）；
4. ``ticket_step`` 只允许列级 UPDATE，整表 UPDATE/DELETE/TRUNCATE 一律不得出现。

新增一张审计类表时，正确做法是把它加进 ``AUDIT_EVIDENCE_TABLES`` 并在迁移里
REVOKE；忘记时这条测试会红，而不是等下一次审计才发现。
"""

from __future__ import annotations

import pytest
from db_support import APP_ROLE, admin_conn, has_pg, run_async

pytestmark = pytest.mark.skipif(
    not has_pg(), reason="未设置 ERP_TEST_DATABASE_URL，跳过权限守卫"
)

WRITE_PRIVS = ("UPDATE", "DELETE", "TRUNCATE")

# 证据类表：链上每一行都必须不可改。新增审计表请加到这里。
AUDIT_EVIDENCE_TABLES = {
    "audit_image": "审计影像链（prev_hash/hash 串链）",
    "audit_anchor": "每日锚点（截至某日的链尾 hash 与条数）",
    "approval_record": "审批记录",
    "config_change": "配置变更记录",
    "ticket_event": "工单事件链（任何结论必须回到这条链重放）",
}

# 允许 erp_app 写、且理由必须写死的表。新增条目请一并写清为什么。
WRITABLE_TABLES = {
    "alembic_version_erp": "迁移版本表，由 alembic 自己维护",
    "ticket": "工单主表：状态机推进与清理都需要 UPDATE/DELETE",
    "ticket_event_anchor": "无条件 upsert 的 O(1) 快捷校验锚点，非证据，可被应用改写",
    "audit_guard_state": "断链后的全局降级开关，必须可 UPDATE",
    "compensation_task": "补偿队列：领取、心跳、终态都靠 UPDATE",
    "job_queue": "任务队列：领取与完成靠 UPDATE",
    "worker_heartbeat": "worker 心跳 upsert",
    "scheduler_lease": "scheduler 单点租约：抢占与过期接管靠 UPDATE",
    "connector_idempotency": "连接器幂等记录：状态推进",
    "connector_breaker_state": "熔断状态：打开/半开/关闭切换",
    "connector_rate_bucket": "限流令牌桶：消费与回填",
}

# 只允许列级 UPDATE 的表（表级写权限必须为无）
COLUMN_SCOPED_TABLES = {
    "ticket_step": "可变执行投影；只授 mark_step 会写的六列，证据只认 ticket_event",
}


async def _table_write_privs(conn) -> dict[str, list[str]]:
    rows = await conn.fetch(
        """
        SELECT c.relname AS name,
               array_remove(ARRAY[
                   CASE WHEN has_table_privilege($1, c.oid, 'UPDATE') THEN 'UPDATE' END,
                   CASE WHEN has_table_privilege($1, c.oid, 'DELETE') THEN 'DELETE' END,
                   CASE WHEN has_table_privilege($1, c.oid, 'TRUNCATE') THEN 'TRUNCATE' END
               ], NULL) AS privs
          FROM pg_class c
         WHERE c.relnamespace = 'erp'::regnamespace
           AND c.relkind = 'r'
         ORDER BY c.relname
        """,
        APP_ROLE,
    )
    return {row["name"]: list(row["privs"]) for row in rows}


def _scan() -> dict[str, list[str]]:
    async def main() -> dict[str, list[str]]:
        conn = await admin_conn()
        try:
            return await _table_write_privs(conn)
        finally:
            await conn.close()

    return run_async(main())


def test_audit_evidence_tables_have_no_write_privilege() -> None:
    privs = _scan()
    offenders = {
        table: sorted(set(privs.get(table, [])))
        for table in AUDIT_EVIDENCE_TABLES
        if privs.get(table)
    }
    assert not offenders, (
        f"{APP_ROLE} 仍持有证据类表的写权限：{offenders}；"
        f"证据类表 = {sorted(AUDIT_EVIDENCE_TABLES)}，必须在迁移里 REVOKE"
    )


def test_every_writable_table_is_registered_with_a_reason() -> None:
    privs = _scan()
    unregistered = sorted(
        table
        for table, granted in privs.items()
        if granted
        and table not in WRITABLE_TABLES
        and table not in COLUMN_SCOPED_TABLES
    )
    assert not unregistered, (
        f"这些表对 {APP_ROLE} 有写权限但未登记理由：{unregistered}；"
        "请判断它是不是证据类表（是 → 加进 AUDIT_EVIDENCE_TABLES 并 REVOKE），"
        "否则在 WRITABLE_TABLES 里逐表写明为什么需要写权限"
    )


def test_registered_writable_tables_are_not_stale() -> None:
    privs = _scan()
    problems = []
    for table, reason in WRITABLE_TABLES.items():
        if table not in privs:
            problems.append(f"{table} 已不存在（登记理由：{reason}）")
        elif not privs[table]:
            problems.append(f"{table} 已无任何写权限（登记理由：{reason}），应删除登记")
    assert not problems, "豁免清单存在失效条目：" + "；".join(problems)


@pytest.mark.parametrize("table", sorted(COLUMN_SCOPED_TABLES))
def test_column_scoped_tables_have_no_table_level_write(table: str) -> None:
    privs = _scan()
    assert table in privs, f"erp.{table} 不存在，迁移未执行"
    assert not privs[table], (
        f"erp.{table} 对 {APP_ROLE} 出现了表级 {privs[table]}；"
        f"它只能有列级 UPDATE（{COLUMN_SCOPED_TABLES[table]}）"
    )
