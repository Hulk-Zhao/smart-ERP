"""S4 审计与权限流。

- ``ledger``：前像/后像、字段级 diff、哈希链、台账检索、每日锚点
- ``rollback``：由审计记录推导反向操作，统一经 WriteGateway 执行并再次留审计
"""

from apps.audit.ledger import (
    AuditChainError,
    AuditLedger,
    AuditQuery,
    ChainVerification,
    InMemorySnapshotStore,
    SnapshotStore,
    compute_field_diff,
)
from apps.audit.rollback import (
    ManualTask,
    RollbackForbidden,
    RollbackItem,
    RollbackKind,
    RollbackPlan,
    RollbackPlanner,
    RollbackRejected,
    RollbackResult,
    RollbackService,
    StepWrite,
)
from apps.audit.store import (
    AuditAnchor,
    AuditStore,
    InMemoryAuditStore,
    SqlAuditStore,
)

__all__ = [
    "AuditAnchor",
    "AuditChainError",
    "AuditLedger",
    "AuditQuery",
    "AuditStore",
    "ChainVerification",
    "InMemoryAuditStore",
    "InMemorySnapshotStore",
    "ManualTask",
    "RollbackForbidden",
    "RollbackItem",
    "RollbackKind",
    "RollbackPlan",
    "RollbackPlanner",
    "RollbackRejected",
    "RollbackResult",
    "RollbackService",
    "SnapshotStore",
    "SqlAuditStore",
    "StepWrite",
    "compute_field_diff",
]
