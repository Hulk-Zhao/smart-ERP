"""文件通道连接器的常量（实体、限流预算、CSV 列口径）。

文件通道是**只读**导入：这里不声明 ``ActionSpec``，因为写回必须经过
``WriteGateway`` 的四路取严，而文件通道目前没有可信的预览/校验/补偿手段。
"""

from __future__ import annotations

from apps.platform.policy.levels import RateLimit

SYSTEM_NAME = "file"
CONNECTOR_VERSION = "csv-v1"

# 实体 → canonical ``Document.kind``（PRD 02 §2.2：单据类型含订单/收货/报工/发票/对账单）。
ENTITY_KINDS: dict[str, str] = {
    "supplier_statement": "supplier_statement",
    "delivery_note": "delivery_note",
}

# CSV 列口径与 S2 对账单行（apps/recon DocLine）对齐，避免同一个字段两种叫法。
REQUIRED_COLUMNS: tuple[str, ...] = ("doc_no", "doc_date", "supplier_code")
OPTIONAL_COLUMNS: tuple[str, ...] = (
    "supplier_name",
    "line_no",
    "item_code",
    "item_name",
    "qty",
    "unit",
    "unit_price",
    "amount",
    "tax_rate",
    "tax_included",
    "currency",
    "period",
    "status",
)
KNOWN_COLUMNS: tuple[str, ...] = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

# 读接口预算（连接器级，不是动作级）：文件读取比 HTTP 便宜，但批量导入会连续扫目录。
READ_LIMIT_KEY = "file.read"
DEFAULT_READ_LIMIT = RateLimit(qps=4.0, daily_quota=2_000, concurrency=2)
