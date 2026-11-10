"""DB 兜底约束存在性：S4 表不污染 S0 基线，CHECK 与代码拒绝同口径。"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from apps.audit.models import AuditAnchorRow, AuditImageRow, AuditBase
from apps.platform.db.models import Base
from apps.security.models import (
    ApprovalRecordRow,
    ConfigChangeRow,
    CredentialLeaseRow,
    SecurityBase,
)


def _ddl(table) -> str:
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def test_s4_tables_do_not_leak_into_s0_baseline():
    baseline = set(Base.metadata.tables)
    assert "erp.audit_image" not in baseline
    assert "erp.audit_anchor" not in baseline
    assert "erp.approval_record" not in baseline
    assert "erp.credential_lease" not in baseline

    assert set(AuditBase.metadata.tables) == {"erp.audit_image", "erp.audit_anchor"}
    assert set(SecurityBase.metadata.tables) == {
        "erp.approval_record",
        "erp.config_change",
        "erp.credential_lease",
    }


def test_approval_record_db_constraints():
    sql = _ddl(ApprovalRecordRow.__table__)
    assert "CHECK (approver_id <> initiator_id)" in sql
    assert "UNIQUE (ticket_id, approver_id)" in sql
    assert "length(btrim(comment)) > 0" in sql
    assert "platform_admin" in sql


def test_audit_image_db_constraints_and_append_only_surface():
    sql = _ddl(AuditImageRow.__table__)
    assert "length(hash) = 64" in sql
    assert "CHECK (length(prev_hash) = 0 OR length(prev_hash) = 64)" in sql
    assert "chain_seq" in sql


def test_config_and_lease_db_constraints():
    config_sql = _ddl(ConfigChangeRow.__table__)
    assert "approver_id IS NULL OR approver_id <> actor_id" in config_sql

    lease_sql = _ddl(CredentialLeaseRow.__table__)
    assert "expires_at > issued_at" in lease_sql
    assert "cardinality(narrowed_fields) > 0" in lease_sql


def test_anchor_schema_has_non_negative_count():
    assert "record_count >= 0" in _ddl(AuditAnchorRow.__table__)
