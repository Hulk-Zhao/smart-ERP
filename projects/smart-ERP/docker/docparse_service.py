#!/usr/bin/env python3
"""doc 容器的临时入口（S5 尚未交付 erp_doc 常驻进程）。

背景：`apps/docparse` 目前是纯库（无 `__main__`、无 OCR 常驻服务、无队列消费），
但 full 档需要一个「能起、能被 healthcheck 探活、镜像里的解析链路确实可用」的
doc 容器。这个入口只用标准库：

- `GET /healthz`：进程自检，不查依赖；
- `GET /readyz`：真跑一次 `DocParsePipeline.parse_bytes`（内联 CSV 夹具），
  管线能跑通返回 200，抛异常返回 503；
- 其他路径：404。

S5 交付真正的 `erp_doc` 常驻进程后，删除本文件、把 docparse.Dockerfile 的 CMD
换成那个入口，并同步 compose 的 doc.command 与 tests/deploy 断言。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_NAME = "smart-erp-doc"
DEFAULT_PORT = 8020
PORT_ENV_KEY = "ERP_DOC_PORT"
# 内联夹具：不依赖镜像外的 fixtures/，用最小「增值税专用发票」验证
# 分类→抽取→映射→校验整条链路（字段齐全时管线返回 ok，不只是「进程活着」）
SELF_CHECK_TEXT = """增值税专用发票
发票号码: 88231007
开票日期: 2026-03-18
购买方名称: 广州华南精密制造有限公司
销售方名称: 东莞市恒泰五金制品有限公司
币种: CNY
序号 | 物料名称 | 单位 | 数量 | 单价 | 金额 | 税率 | 税额
1 | 不锈钢螺钉M6 | 个 | 1000 | 0.85 | 850.00 | 13% | 110.50
小计: 850.00
税额合计: 110.50
价税合计: 960.50
""".encode()


def readiness() -> tuple[bool, dict]:
    """跑一次解析管线；任何异常都视为未就绪并给出原因。"""
    try:
        from apps.docparse.models import ParseResult
        from apps.docparse.pipeline import PARSER_ID, DocParsePipeline

        result: ParseResult = DocParsePipeline().parse_bytes(
            SELF_CHECK_TEXT, filename="selfcheck.txt"
        )
        return True, {
            "status": "ready",
            "service": SERVICE_NAME,
            "parser": PARSER_ID,
            "selfcheck_status": getattr(result.status, "value", str(result.status)),
            "selfcheck_doc_type": getattr(
                result.doc_type, "value", str(result.doc_type)
            ),
        }
    except Exception as exc:  # noqa: BLE001 - 就绪探针不允许抛
        return False, {
            "status": "not_ready",
            "service": SERVICE_NAME,
            "error": f"{type(exc).__name__}: {exc}",
        }


class Handler(BaseHTTPRequestHandler):
    server_version = SERVICE_NAME

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"status": "ok", "service": SERVICE_NAME})
        elif self.path == "/readyz":
            ok, payload = readiness()
            self._send(200 if ok else 503, payload)
        else:
            self._send(404, {"status": "not_found", "path": self.path})

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write(f"{self.address_string()} - {format % args}\n")


def port_from_env() -> int:
    raw = os.environ.get(PORT_ENV_KEY, "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{PORT_ENV_KEY}={raw!r} 不是合法端口") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{PORT_ENV_KEY}={port} 超出 1..65535")
    return port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docparse_service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--check", action="store_true", help="只跑就绪自检后退出")
    args = parser.parse_args(argv)

    ok, payload = readiness()
    if args.check:
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if ok else 2
    if not ok:
        print(f"doc 启动失败: {payload.get('error')}", file=sys.stderr)
        return 2

    try:
        port = args.port if args.port is not None else port_from_env()
        if not 1 <= port <= 65535:
            raise ValueError(f"端口 {port} 超出 1..65535")
    except ValueError as exc:
        print(f"doc 启动失败: {exc}", file=sys.stderr)
        return 2

    server = ThreadingHTTPServer((args.host, port), Handler)
    print(f"{SERVICE_NAME} 监听 {args.host}:{port}（自检 {payload['selfcheck_status']}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
