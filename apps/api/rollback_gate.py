"""同步回滚入口开关（POC 只读期默认关闭）。

为什么默认关：``POST /tickets/{id}/actions/compensate`` 目前走 ``RollbackService``
同步回写，既不在 ``ticket_event`` 留痕，也没有租约/重试；外部写成功与审计影像
落库之间还有两段窗口。在补偿执行器路径接上之前，宁可明确报「不可用」，也不要
让线上留着一条会**无痕跨系统写**的路径。

读法与 ``apps/api/price_book.require_contract_price`` 同口径：按请求时读环境变量，
测试可注入 ``env`` 映射而不改进程环境；空串/未配置一律视为 false。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

ENV_ROLLBACK_SYNC_ENABLED = "ERP_ROLLBACK_SYNC_ENABLED"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def sync_rollback_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``ERP_ROLLBACK_SYNC_ENABLED`` 为真才开放同步回滚；默认 false。"""

    source = os.environ if env is None else env
    return (source.get(ENV_ROLLBACK_SYNC_ENABLED) or "").strip().lower() in _TRUTHY
