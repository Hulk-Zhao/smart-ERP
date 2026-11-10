"""文件通道连接器（T105）：对账单/送货单 CSV 的只读导入。

对外只暴露 SDK 契约（``FileConnector`` 继承 ``BaseConnector``）与装配入口；
``transport`` 是包内私有实现，写回不存在。
"""

from __future__ import annotations

from apps.connectors.file.connector import FileConnector
from apps.connectors.file.factory import FileStack, build_file_stack
from apps.connectors.file.spec import ENTITY_KINDS, SYSTEM_NAME

__all__ = ["ENTITY_KINDS", "SYSTEM_NAME", "FileConnector", "FileStack", "build_file_stack"]
