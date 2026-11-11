"""`erp_doc` 包内入口（apps.docparse.service）的验收：健康/就绪/404/--check。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from apps.docparse.service import Handler, SERVICE_NAME, main, readiness


def test_readiness_runs_real_pipeline():
    """就绪探针必须真跑解析管线，而不是只证明进程活着。"""
    ok, payload = readiness()
    assert ok, payload
    assert payload["status"] == "ready"
    assert payload["parser"]
    assert payload["selfcheck_status"]
    assert payload["selfcheck_doc_type"]


def _serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_endpoints():
    server = _serve()
    try:
        port = server.server_address[1]
        code, body = _get(f"http://127.0.0.1:{port}/healthz")
        assert (code, body["status"], body["service"]) == (200, "ok", SERVICE_NAME)

        code, body = _get(f"http://127.0.0.1:{port}/readyz")
        assert code == 200, body
        assert body["status"] == "ready"

        code, body = _get(f"http://127.0.0.1:{port}/nope")
        assert (code, body["status"]) == (404, "not_found")
    finally:
        server.shutdown()
        server.server_close()


def test_check_mode_exits_zero(capsys):
    """镜像/CI 用 `--check` 做一次性自检：就绪退出 0。"""
    assert main(["--check"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "ready"
