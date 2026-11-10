"""S1 连接器 SDK：能力声明 + 运行时韧性（限流/重试/幂等/熔断）+ 统一数据模型映射。

与 S0 的分工（不重复造契约）：
- ``ActionSpec`` / ``ConnectorMeta`` / ``CapabilitySet`` / ``BaseConnector`` 由
  ``apps.platform.connectors.sdk`` 与 ``apps.platform.policy.levels`` 冻结；
- 本包只补连接器**实现侧**的通用能力，以及首个厂商连接器（U8 假实现）。

写路径的唯一出口仍是 ``WriteGateway`` → ``BaseConnector.write()`` → transport；
本包不允许出现第二个直接调用 transport 的公开方法（见 ``tests/connectors``）。
"""

from __future__ import annotations

from apps.connectors.errors import (
    ConnectorError,
    ErrorClass,
    ErrorKind,
    classify_exception,
    classify_status_code,
)

__all__ = [
    "ConnectorError",
    "ErrorClass",
    "ErrorKind",
    "classify_exception",
    "classify_status_code",
]
