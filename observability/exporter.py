"""本地 JSON Lines 导出器；不连外网、不依赖第三方库。

开关（全部 ``ERP_`` 前缀，与平台配置一致）：

- ``ERP_TRACE_ENABLED``：``1/true/on`` 才导出，默认关闭；
- ``ERP_TRACE_FILE``：导出文件路径，默认 ``./logs/traces.jsonl``。

导出失败不得影响业务：``export()`` 吞掉 IO 异常并计数，由 :attr:`JsonlExporter.errors`
读取；埋点坏了不能把工单执行拖死。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Protocol

from observability.trace import Span

DEFAULT_TRACE_FILE = "logs/traces.jsonl"


class SpanExporter(Protocol):
    def export(self, span: Span) -> None: ...


class JsonlExporter:
    """追加写 JSONL；每行一个 span，失败只累计错误数。"""

    def __init__(self, path: str | os.PathLike[str], *, fsync: bool = False) -> None:
        self._path = Path(path)
        self._fsync = fsync
        self._lock = threading.Lock()
        self._errors: list[str] = []

    @property
    def path(self) -> Path:
        return self._path

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(self._errors)

    def export(self, span: Span) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(span.to_dict(), ensure_ascii=False, separators=(",", ":"))
            with self._lock, self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                if self._fsync:
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:  # pragma: no cover - 磁盘异常不可在单测里稳定复现
            self._errors.append(f"{type(exc).__name__}: {exc}")


class MemoryExporter:
    """测试与本地调试用；保留全部 span 快照。"""

    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def export(self, span: Span) -> None:
        with self._lock:
            self.spans.append(span.to_dict())


def exporter_from_env(env: dict[str, str] | None = None) -> SpanExporter | None:
    """按环境变量装配导出器；未开启返回 ``None``（纯内存）。"""

    values = env if env is not None else dict(os.environ)
    if values.get("ERP_TRACE_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    path = values.get("ERP_TRACE_FILE", "").strip() or DEFAULT_TRACE_FILE
    return JsonlExporter(path)
