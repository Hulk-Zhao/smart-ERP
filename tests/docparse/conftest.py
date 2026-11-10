"""pytest 路径与公共夹具。

仓库尚未安装为包，测试通过把仓库根加入 sys.path 后以 ``apps.docparse`` 导入。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "fixtures" / "docparse"


@pytest.fixture()
def pipeline():
    from apps.docparse import DocParsePipeline

    return DocParsePipeline()


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES
