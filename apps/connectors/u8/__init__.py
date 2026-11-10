"""U8 连接器（假实现 + 映射 + 本地 mock）。

不连接真实 U8：传输层打本地 mock，测试用 ``httpx.MockTransport`` 注入。
"""

from __future__ import annotations

from apps.connectors.u8.actions import ACTIONS, ACTIONS_BY_KEY, READ_LIMIT_KEY
from apps.connectors.u8.connector import U8Connector
from apps.connectors.u8.factory import U8Stack, build_u8_stack
from apps.connectors.u8.mock import MockU8Backend

__all__ = [
    "ACTIONS",
    "ACTIONS_BY_KEY",
    "READ_LIMIT_KEY",
    "MockU8Backend",
    "U8Connector",
    "U8Stack",
    "build_u8_stack",
]
