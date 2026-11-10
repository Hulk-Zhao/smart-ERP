"""防复发：生产 ``create_app()`` 必须真的暴露 17 条 C16 业务路径。

本用例刻意在**子进程**里只 import ``apps.main.create_app``，不 import
``apps.api`` 任何模块，也不经过 tests/api 的夹具。这样能抓到「路由代码写了、
但没接进启动路径」——在当前进程 import 过业务模块时，路由会被提前挂上，测不出问题。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = "__C16_PATHS__"

# 与 apps/api/contract.py 的路径集合保持一致（去掉 /erp/api 前缀后的路由路径）。
EXPECTED_PATHS = (
    "/v1/tickets",
    "/v1/tickets/{ticket_id}",
    "/v1/tickets/{ticket_id}/actions/approve",
    "/v1/tickets/{ticket_id}/actions/reject",
    "/v1/tickets/{ticket_id}/actions/cancel",
    "/v1/tickets/{ticket_id}/actions/compensate",
    "/v1/recon/runs",
    "/v1/recon/runs/{batch_id}",
    "/v1/recon/runs/{batch_id}/diffs",
    "/v1/recon/runs/{batch_id}/diffs/{diff_id}",
    "/v1/approvals/pending",
    "/v1/approvals/{ticket_id}/approve",
    "/v1/approvals/{ticket_id}/reject",
    "/v1/audit/tickets/{ticket_id}/images",
    "/v1/audit/tickets/{ticket_id}/chain",
    "/v1/audit/export",
    "/v1/metering/batches/{batch_id}/statement",
)

_SCRIPT = """
import json

from apps.main import create_app
from apps.platform.config import Settings

app = create_app(Settings(image_tag="test"))
print("__C16_PATHS__" + json.dumps(sorted(app.openapi()["paths"])))
"""


def _production_paths() -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    line = next(
        (row for row in proc.stdout.splitlines() if row.startswith(MARKER)), None
    )
    assert line is not None, proc.stdout
    return json.loads(line[len(MARKER) :])


def test_production_app_exposes_all_c16_paths():
    paths = _production_paths()
    missing = [path for path in EXPECTED_PATHS if path not in paths]

    assert missing == [], (
        "create_app() 得不到业务路由：模块写了但没接进启动路径。"
        f" missing={missing} actual={paths}"
    )
    assert len(EXPECTED_PATHS) == 17
    assert all(path.startswith("/v1/") for path in EXPECTED_PATHS)
    assert not any(path.startswith("/_e2e") for path in paths)
