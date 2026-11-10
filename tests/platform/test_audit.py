"""审计影像哈希链。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from apps.platform.contracts.audit import (
    AuditImage,
    FieldDiffEntry,
    compute_image_hash,
    verify_chain,
)


def _image(prev_hash: str, before: str, after: str) -> AuditImage:
    payload = {
        "id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "ticket_id": str(uuid4()),
        "step_id": str(uuid4()),
        "system": "kingdee",
        "object_type": "draft",
        "object_id": "D-001",
        "before_ref": f"img://{before}",
        "after_ref": f"img://{after}",
        "field_diff": [{"field": "amount", "before": "10.0000", "after": "12.0000"}],
        "diff_missing": False,
        "prev_hash": prev_hash,
        "hash": "0" * 64,
        "created_at": datetime(2026, 11, 10, tzinfo=timezone.utc),
    }
    image = AuditImage.model_validate(payload)
    digest = compute_image_hash(prev_hash, image.model_dump(mode="python"))
    return image.model_copy(update={"hash": digest})


def test_chain_verifies() -> None:
    first = _image("", "before1", "after1")
    second = _image(first.hash, "before2", "after2")

    assert verify_chain([first, second]) is True


def test_tampered_image_fails() -> None:
    first = _image("", "before1", "after1")
    second = _image(first.hash, "before2", "after2")
    tampered = second.model_copy(
        update={"field_diff": [FieldDiffEntry(field="amount", before="10.0000", after="99.0000")]}
    )

    assert verify_chain([first, tampered]) is False


def test_hash_excludes_self_reference() -> None:
    first = _image("", "before1", "after1")
    payload = first.model_dump(mode="python")
    assert compute_image_hash("", payload) == first.hash
