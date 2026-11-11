"""种子审计链守卫。

demo 数据集里的 ``erp.audit_image`` 由生成器直接产出；哈希必须由**生产函数**
（``apps.platform.contracts.audit.compute_image_hash``）算出，否则「种子造的链」
和「系统验的链」会悄悄漂移——演示现场一打开工单详情就报「疑似篡改」。

本测试做两件事：
1. 逐条重算 hash，必须与种子里的 ``hash`` 完全一致；
2. 断言 ``created_at`` 是 UTC。生产链在 PG 往返后面临 asyncpg 的 UTC 归一化，
   种子若用 +08:00 参与哈希，同一时刻会得到另一个 digest。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from apps.platform.contracts.audit import AuditImage, compute_image_hash

SEED_DIR = Path(__file__).resolve().parents[2] / "demo" / "scenarios"
SEEDS = sorted(SEED_DIR.glob("*/erp_seed.json"))


def test_seed_directories_present() -> None:
    assert SEEDS, f"未找到任何 demo 种子：{SEED_DIR}"


@pytest.mark.parametrize("seed_path", SEEDS, ids=lambda path: path.parent.name)
def test_seed_audit_chain_matches_production_hash(seed_path: Path) -> None:
    rows = json.loads(seed_path.read_text(encoding="utf-8"))["audit_images"]
    assert rows, f"{seed_path.parent.name} 没有审计影像，演示将看不到审计链"

    prev = ""
    for index, row in enumerate(rows):
        image = AuditImage.model_validate(row)
        assert image.created_at.utcoffset() == timedelta(0), (
            f"{seed_path.parent.name} 第 {index} 条 created_at 必须是 UTC："
            f"{image.created_at.isoformat()}（PG 往返后 asyncpg 按 UTC 返回）"
        )
        assert image.prev_hash == prev, (
            f"{seed_path.parent.name} 第 {index} 条 prev_hash 断链："
            f"{image.prev_hash!r} != {prev!r}"
        )
        recomputed = compute_image_hash(prev, image.model_dump(mode="python"))
        assert recomputed == image.hash, (
            f"{seed_path.parent.name} 第 {index} 条 hash 与生产函数不一致："
            f"种子 {image.hash} / 重算 {recomputed}"
        )
        prev = image.hash
