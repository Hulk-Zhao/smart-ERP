"""U8 本地桩：**模拟外部系统**，用于演示「跨系统写入 + 可审计回滚」。

这不是 U8 / 任何真实 ERP。启动横幅、响应体 ``simulated`` 字段、
``X-Simulated-System: u8-stub`` 响应头都明确标注「模拟系统」——
禁止把它当成真实对接，也禁止在生产环境指到它。

启动::

    python -m apps.connectors.u8.stub_server --host 127.0.0.1 --port 8013

然后把 ``ERP_U8_BASE_URL`` 指到 ``http://127.0.0.1:8013``。它实现
``apps/connectors/u8/transport.py`` 需要的写/读/删三条路径：

- ``POST /u8/api/purchase-order-drafts`` 建草稿，返回单号 + 时间戳 + 关键字段回显；
- ``GET  /u8/api/purchase-order-drafts?source_doc_no=...`` 供 verify 回查；
- ``DELETE /u8/api/purchase-order-drafts/{单号}`` 供补偿冲销；
- ``GET /stub/receipts`` / ``GET /stub/reversals`` 是对外核对入口（钱/单据到没到）；
- ``GET /stub/requests`` 是全部请求留痕（判零外部写入：只数 ``POST``/``DELETE`` 且路径不以
  ``/stub/`` 开头的条目；GET 探测与健康检查不计入）。

故障注入（用于演示失败与补偿，不是正常业务接口）::

    POST /stub/fault {"fail_posts": 1, "delay_seconds": 0}
    POST /stub/reset
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

SIMULATED_SYSTEM = "u8-stub（模拟外部系统，不是真实 U8）"
SIMULATED_HEADER = "u8-stub"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8013

#: 写路由 → 单号前缀（与 apps/connectors/u8/transport.py 的 DRAFT_ROUTES 对齐）。
_DRAFT_ROUTES: dict[str, str] = {
    "purchase-order-drafts": "PO",
    "arrival-drafts": "AR",
}
_READ_ROUTES = frozenset({"suppliers", "items", "purchase-orders", "arrival-vouches"})


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class StubState:
    """桩的全部可观测状态：请求流水、回执、冲销、故障开关。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self.reversals: list[dict[str, Any]] = []
        self.fail_posts = 0
        self.delay_seconds = 0.0
        self._seq = 0
        self._by_idem: dict[str, dict[str, Any]] = {}

    def record(self, method: str, path: str) -> None:
        with self.lock:
            self.requests.append({"method": method, "path": path, "at": _now()})

    def next_code(self, prefix: str) -> str:
        with self.lock:
            self._seq += 1
            return f"{prefix}-STUB-{self._seq:06d}"

    def replay(self, idem_key: str | None) -> dict[str, Any] | None:
        if not idem_key:
            return None
        with self.lock:
            receipt = self._by_idem.get(idem_key)
            return dict(receipt) if receipt is not None else None

    def remember(self, idem_key: str | None, receipt: dict[str, Any]) -> None:
        if idem_key:
            with self.lock:
                self._by_idem[idem_key] = receipt

    def add_receipt(self, receipt: dict[str, Any]) -> None:
        with self.lock:
            self.receipts.append(receipt)

    def take_failure(self) -> bool:
        with self.lock:
            if self.fail_posts <= 0:
                return False
            self.fail_posts -= 1
            return True

    def find_receipt(self, code: str) -> dict[str, Any] | None:
        with self.lock:
            for receipt in self.receipts:
                if receipt["cCode"] == code:
                    return receipt
        return None

    def reverse(self, code: str) -> dict[str, Any] | None:
        with self.lock:
            for index, receipt in enumerate(self.receipts):
                if receipt["cCode"] == code:
                    removed = self.receipts.pop(index)
                    reversal = {
                        "cCode": code,
                        "source_doc_no": removed.get("source_doc_no"),
                        "reverted_at": _now(),
                        "simulated": True,
                        "system": SIMULATED_SYSTEM,
                    }
                    self.reversals.append(reversal)
                    return reversal
        return None

    def reset(self) -> None:
        """回到干净起点：等同于一次新进程启动，单号从 000001 重新开始。"""
        with self.lock:
            self.requests.clear()
            self.receipts.clear()
            self.reversals.clear()
            self._by_idem.clear()
            self.fail_posts = 0
            self.delay_seconds = 0.0
            self._seq = 0


class _Handler(BaseHTTPRequestHandler):
    server_version = "SmartERP-U8Stub/1.0 (SIMULATED)"
    sys_version = ""

    @property
    def state(self) -> StubState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[u8-stub 模拟外部系统] {format % args}")

    # ------------------------------------------------------------------ 响应
    def _send(self, status: int, body: Any = None, *, headers: dict[str, str] | None = None) -> None:
        payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("X-Simulated-System", SIMULATED_HEADER)
        if body is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        self.state.record("GET", self.path)
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/stub/healthz":
            self._send(200, {"ok": True, "simulated": True, "system": SIMULATED_SYSTEM})
            return
        if path == "/stub/receipts":
            self._send(200, {"simulated": True, "items": list(self.state.receipts)})
            return
        if path == "/stub/reversals":
            self._send(200, {"simulated": True, "items": list(self.state.reversals)})
            return
        if path == "/stub/requests":
            # 判零外部写入：只数 POST/DELETE 且路径不以 /stub/ 开头的条目（GET 探测不计入）。
            self._send(200, {"simulated": True, "items": list(self.state.requests)})
            return

        prefix = "/u8/api/"
        if not path.startswith(prefix):
            self._send(404, {"error": "not_found", "path": path})
            return
        route = path[len(prefix) :]
        if route in _DRAFT_ROUTES:
            source_doc_no = (parse_qs(parsed.query).get("source_doc_no") or [""])[0]
            rows = [
                receipt
                for receipt in self.state.receipts
                if not source_doc_no or receipt.get("source_doc_no") == source_doc_no
            ]
            self._send(200, {"data": rows})
            return
        if route in _READ_ROUTES:
            self._send(200, {"data": []})
            return
        self._send(404, {"error": "not_found", "route": route})

    def do_POST(self) -> None:  # noqa: N802
        self.state.record("POST", self.path)
        path = urlparse(self.path).path.rstrip("/")

        if path == "/stub/fault":
            body = self._read_json()
            self.state.fail_posts = int(body.get("fail_posts") or 0)
            self.state.delay_seconds = float(body.get("delay_seconds") or 0.0)
            self._send(200, {"ok": True, "fail_posts": self.state.fail_posts})
            return
        if path == "/stub/reset":
            self.state.reset()
            self._send(200, {"ok": True})
            return

        prefix = "/u8/api/"
        if not path.startswith(prefix):
            self._send(404, {"error": "not_found", "path": path})
            return
        route = path[len(prefix) :]
        prefix_code = _DRAFT_ROUTES.get(route)
        if prefix_code is None:
            self._send(404, {"error": "not_found", "route": route})
            return

        if self.state.delay_seconds:
            time.sleep(self.state.delay_seconds)
        if self.state.take_failure():
            self._send(
                503,
                {
                    "error": "simulated_failure",
                    "simulated": True,
                    "system": SIMULATED_SYSTEM,
                    "detail": "桩按故障注入返回 503，用于验证失败/补偿路径",
                },
            )
            return

        body = self._read_json()
        idem_key = self.headers.get("Idempotency-Key")
        existing = self.state.replay(idem_key)
        if existing is not None:
            self._send(200, existing, headers={"Idempotent-Replay": "true"})
            return

        code = self.state.next_code(prefix_code)
        receipt = {
            "cCode": code,
            "source_doc_no": body.get("source_doc_no"),
            "cVenCode": body.get("cVenCode"),
            "dPODate": body.get("dPODate"),
            "cMemo": body.get("cMemo"),
            "details": body.get("details") or [],
            "line_count": len(body.get("details") or []),
            "received_at": _now(),
            "simulated": True,
            "system": SIMULATED_SYSTEM,
        }
        self.state.add_receipt(receipt)
        self.state.remember(idem_key, receipt)
        self._send(201, receipt)

    def do_DELETE(self) -> None:  # noqa: N802
        self.state.record("DELETE", self.path)
        path = urlparse(self.path).path.rstrip("/")
        prefix = "/u8/api/"
        if not path.startswith(prefix):
            self._send(404, {"error": "not_found", "path": path})
            return
        parts = path[len(prefix) :].split("/")
        if len(parts) != 2 or parts[0] not in _DRAFT_ROUTES:
            self._send(404, {"error": "not_found", "path": path})
            return
        reversal = self.state.reverse(parts[1])
        if reversal is None:
            self._send(404, {"error": "draft_not_found", "cCode": parts[1]})
            return
        self._send(204, None)


class StubU8Server:
    """可直接嵌进测试的桩：``with StubU8Server() as stub: stub.base_url``。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = 0) -> None:
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.state = StubState()  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> StubState:
        return self._httpd.state  # type: ignore[attr-defined]

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> StubU8Server:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> StubU8Server:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="U8 本地桩（模拟外部系统，不是真实 U8）")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    server = StubU8Server(args.host, args.port).start()
    print("=" * 72)
    print("  ⚠  模拟外部系统（u8-stub）——不是真实 U8 / ERP 对接")
    print(f"  ⚠  仅用于演示「跨系统写入 + 可审计回滚」，禁止用于生产")
    print(f"  监听: {server.base_url}")
    print(f"  回执核对: {server.base_url}/stub/receipts")
    print(f"  冲销核对: {server.base_url}/stub/reversals")
    print("=" * 72)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
