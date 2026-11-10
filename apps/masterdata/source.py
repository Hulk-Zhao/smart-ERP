"""只读取数接口。

未来接连接器时，实现方只能提供快照，不得调用任何写接口（W0）。
本文件刻意不连接任何系统，也不包含写回方法。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .loader import load_snapshot
from .models import MasterDataSnapshot


class MasterDataSource(Protocol):
    def load(self) -> MasterDataSnapshot:
        """返回一次体检的只读快照；实现方不得写回任何系统。"""
        ...


@dataclass(frozen=True)
class JsonFileSource:
    """本地 JSON 快照源，用于离线体检与测试。"""

    path: Path

    def load(self) -> MasterDataSnapshot:
        return load_snapshot(self.path)
