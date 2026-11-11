"""租户绑定的系统级守卫：仓储读方法必须把 ``tenant_id`` 作为参数。

这比逐条端点扫描耐久：新增一个仓储读方法却忘了租户，会在这里直接变红，
而不是等到某天有人扫到第 18 条路径。
"""

from __future__ import annotations

import dataclasses
import inspect

from apps.api.recon_store import FileReconStore, InMemoryReconStore, SqlReconStore
from apps.api.store import ReconBatchRepository, TicketRepository
from apps.audit.store import AuditQuery

#: 写入/登记不是「按租户读资源」，不要求 tenant_id。
MUTATING_METHODS = frozenset({"add", "create", "save", "register"})


def _read_methods(repository: type) -> list[tuple[str, object]]:
    return [
        (name, member)
        for name, member in inspect.getmembers(repository, inspect.isfunction)
        if not name.startswith("_") and name not in MUTATING_METHODS
    ]


def test_every_repository_read_method_declares_tenant_id() -> None:
    """面向资源的仓储读方法必须声明 ``tenant_id``；漏一个即失败。"""

    missing = [
        f"{repository.__name__}.{name}"
        for repository in (TicketRepository, ReconBatchRepository)
        for name, method in _read_methods(repository)
        if "tenant_id" not in inspect.signature(method).parameters
    ]
    assert missing == [], f"读方法缺少 tenant_id: {missing}"


def test_ticket_read_methods_require_tenant_id() -> None:
    """工单仓储的租户参数必须必填且仅限关键字，不能有「省略即全租户」的默认值。"""

    for name, method in _read_methods(TicketRepository):
        parameter = inspect.signature(method).parameters["tenant_id"]
        assert parameter.default is inspect.Parameter.empty, f"{name} 的 tenant_id 不得有默认值"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, f"{name} 的 tenant_id 必须关键字传入"


def test_recon_store_load_requires_tenant_id() -> None:
    """对账快照加载必须同时给批次与租户，才能命中持久化层。"""

    for store in (InMemoryReconStore, FileReconStore, SqlReconStore):
        parameter = inspect.signature(store.load).parameters["tenant_id"]
        assert parameter.default is inspect.Parameter.empty, f"{store.__name__}.load 缺少必填 tenant_id"


def test_audit_query_requires_tenant_id() -> None:
    """审计查询对象必须携带租户，且不可省略。"""

    field = dataclasses.fields(AuditQuery)
    tenant = next(item for item in field if item.name == "tenant_id")
    assert tenant.default is dataclasses.MISSING, "AuditQuery.tenant_id 不得有默认值"
