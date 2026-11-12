#!/usr/bin/env python3
"""确定性生成 smart-ERP 三套演示数据集（标准库 + 生产审计契约）。

审计影像的 ``hash`` 由生产函数 ``apps.platform.contracts.audit.compute_image_hash``
计算，禁止在本文件内另写一份算法：种子造的链必须与系统验的链同源。

产出（全部入库，云服务器直接取仓库）：
    demo/scenarios/<scenario>/*.csv        业务数据（主数据/交易/对账/工单案例）
    demo/scenarios/<scenario>/manifest.json 元数据、行数、勾稽结论
    demo/scenarios/<scenario>/erp_seed.json 产品 API 直接消费的 erp.* 投影

约束（生成时强制）：
- 时间只落在过去 12 个月（2025-11-12 .. 2026-11-10），业务顺序单调；
- 订单/入库/发票/对账/工单逐层勾稽，金额两位小数、行合计等于头；
- 差异类型覆盖契约全部 8 类；工单状态覆盖 12 类中的展示子集，含两条补偿闭环；
- 命名不带「测试 / test / abc / 样例」；
- 同一 seed 重复执行输出逐字节一致。

用法：
    python demo/tools/build_datasets.py           # 生成三套
    python demo/tools/build_datasets.py --scenario manufacturing-multi-plant
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 生产哈希口径唯一来源（与 apps/audit/ledger.py 追加时同一套函数）。
from apps.audit.ledger import compute_field_diff  # noqa: E402
from apps.api.recon_snapshot import UnitPriceSource, snapshot_from_batch  # noqa: E402
from apps.api.service import _batch_view, _metrics_out, _missing_out  # noqa: E402
from apps.api.store import ReconBatchRecord  # noqa: E402
from apps.platform.contracts.audit import (  # noqa: E402
    AuditImage,
    compute_image_hash,
)
from apps.recon.engine import reconcile  # noqa: E402
from apps.recon.loader import build_request  # noqa: E402

SCEN_DIR = ROOT / "scenarios"
TODAY = date(2026, 11, 11)
WINDOW_START = TODAY - timedelta(days=364)
TZ = timezone(timedelta(hours=8))

DEMO_TENANT = UUID("11111111-1111-1111-1111-111111111111")
USERS = {
    "initiator": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    "reviewer": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    "approver": UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
    "cfo": UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
}
USER_NAMES = {
    "initiator": "沈立诚（对账运营）",
    "reviewer": "顾清和（财务复核）",
    "approver": "陆明远（业务负责人）",
    "cfo": "宋知微（财务负责人）",
}

DIFF_OWNER = {
    "unit_price": "supplier",
    "qty": "supplier",
    "scrap": "supplier",
    "uom": "supplier",
    "tax": "system",
    "missing_doc": "supplier",
    "duplicate": "supplier",
    "rounding_tolerance": "system",
}

TIER1 = "一级经销"
TIER2 = "二级经销"
KA = "KA直营"


def money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money4(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def rate4(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def iso(value: date) -> str:
    return value.isoformat()


def ts(value: datetime) -> str:
    return value.astimezone(TZ).isoformat()


def daterange(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, max(0, (end - start).days)))


def cap_today(value: date) -> date:
    """计划类日期（承诺日/到期日/结算日）一律不越过今天。"""
    return min(value, TODAY)


def stable_seed(code: str) -> int:
    return int(hashlib.sha256(code.encode("utf-8")).hexdigest()[:12], 16)


def uid(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "smart-erp-demo/" + "/".join(parts))


# --------------------------------------------------------------------------- 配置


@dataclass(frozen=True)
class Scenario:
    code: str
    display_name: str
    industry: str
    story: str
    base_currency: str
    orgs: tuple[dict, ...]
    warehouses: tuple[dict, ...]
    currencies: tuple[dict, ...]
    taxes: tuple[dict, ...]
    customer_kind: str
    supplier_kinds: tuple[str, ...]
    item_counts: tuple[int, int, int, int]  # FG RM PK SF
    so_count: int
    so_line_range: tuple[int, int]
    po_count: int
    po_line_range: tuple[int, int]
    mo_count: int
    periods: tuple[str, ...]
    recon_batches: int
    scenario_extra: str
    customer_words: tuple[str, ...]
    supplier_words: tuple[str, ...]
    item_words: dict[str, tuple[str, ...]]
    item_categories: dict[str, tuple[str, ...]]  # 与 item_words 逐项对齐的三字母分类段
    regions: tuple[tuple[str, str], ...]


def _mfg() -> Scenario:
    return Scenario(
        code="manufacturing-multi-plant",
        display_name="华辰精密制造集团 · 多工厂协同",
        industry="离散制造（汽车零部件 / 精密传动）",
        story="苏州总装、宁波精工、武汉电子三地工厂共用一套主数据；两层以上 BOM 驱动生产工单领料，"
        "采购收货按批次入库并形成库龄，月末与 80 家供应商对账，差异走工单审批与补偿回滚。",
        base_currency="CNY",
        orgs=(
            {"org_code": "ORG-SZ", "org_name": "苏州总装厂", "legal_entity": "苏州华辰精密制造有限公司", "country": "CN", "base_currency": "CNY"},
            {"org_code": "ORG-NB", "org_name": "宁波精工园", "legal_entity": "宁波华辰精工科技有限公司", "country": "CN", "base_currency": "CNY"},
            {"org_code": "ORG-WH", "org_name": "武汉电子厂", "legal_entity": "武汉华辰电子组件有限公司", "country": "CN", "base_currency": "CNY"},
        ),
        warehouses=(
            {"wh_code": "WH-SZ-01", "org_code": "ORG-SZ", "wh_name": "苏州成品仓", "wh_type": "成品仓", "province": "江苏省", "city": "苏州市", "address": "苏州工业园区星湖街 218 号 B2 库"},
            {"wh_code": "WH-SZ-02", "org_code": "ORG-SZ", "wh_name": "苏州原料仓", "wh_type": "原料仓", "province": "江苏省", "city": "苏州市", "address": "苏州工业园区唯新路 58 号 3 号库"},
            {"wh_code": "WH-NB-01", "org_code": "ORG-NB", "wh_name": "宁波半成品仓", "wh_type": "半成品仓", "province": "浙江省", "city": "宁波市", "address": "宁波市北仑区大碶街道沿山河南路 77 号"},
            {"wh_code": "WH-NB-02", "org_code": "ORG-NB", "wh_name": "宁波包材仓", "wh_type": "包材仓", "province": "浙江省", "city": "宁波市", "address": "宁波市鄞州区姜山镇工业园 12 幢"},
            {"wh_code": "WH-WH-01", "org_code": "ORG-WH", "wh_name": "武汉电子料仓", "wh_type": "原料仓", "province": "湖北省", "city": "武汉市", "address": "武汉市东湖新技术开发区高新四路 6 号"},
        ),
        currencies=(
            {"currency": "CNY", "currency_name": "人民币", "rate_to_cny": "1.000000"},
            {"currency": "USD", "currency_name": "美元", "rate_to_cny": "7.118000"},
        ),
        taxes=(
            {"tax_code": "VAT13", "tax_name": "增值税 13%", "country": "CN", "rate": "0.130000"},
            {"tax_code": "VAT9", "tax_name": "增值税 9%", "country": "CN", "rate": "0.090000"},
            {"tax_code": "VAT6", "tax_name": "增值税 6%", "country": "CN", "rate": "0.060000"},
        ),
        customer_kind="mfg",
        supplier_kinds=("STL", "ALU", "ELE", "PKG", "CHE"),
        item_counts=(260, 220, 80, 40),
        so_count=420,
        so_line_range=(4, 7),
        po_count=260,
        po_line_range=(2, 4),
        mo_count=220,
        periods=("2026-05", "2026-06", "2026-07", "2026-08", "2026-09"),
        recon_batches=3,
        scenario_extra="manufacturing",
        customer_words=(
            "华域", "延锋", "均胜", "拓普", "旭升", "银轮", "三花", "万向", "潍柴", "玉柴",
            "格力", "美的", "海尔", "海信", "三一", "徐工", "中联", "柳工", "长城", "吉利",
            "比亚迪", "奇瑞", "长安", "江淮", "宇通", "金龙", "奥克斯", "格兰仕", "九阳", "苏泊尔",
        ),
        supplier_words=(
            "宝山钢铁", "鞍山钢铁", "沙钢集团", "首钢京唐", "太原钢铁", "马鞍山钢铁", "华菱湘钢", "南钢股份",
            "永钢集团", "中天钢铁", "中国铝业", "南山铝业", "云南铝业", "神火集团", "中孚实业", "明泰铝业",
            "立讯精密", "歌尔股份", "瑞声科技", "舜宇光学", "京东方", "深天马", "汇顶科技", "韦尔股份",
            "兆易创新", "士兰微", "裕同科技", "合兴包装", "美盈森", "劲嘉股份", "紫江企业", "奥瑞金",
            "万华化学", "金发科技", "巴斯夫中国", "陶氏化学", "科思创", "三菱化学", "东丽塑料", "帝斯曼工程材料",
        ),
        item_words={
            "FG": ("前副车架总成", "转向节锻件", "精密齿轮组件", "电机定子总成", "液压阀体", "传动轴总成", "制动卡钳支架", "悬架控制臂"),
            "RM": ("冷轧钢板", "铝锭", "铜棒", "工程塑料粒子", "稀土磁钢", "轴承钢", "密封圈", "电子元器件"),
            "PK": ("出口木托盘", "防锈气相纸", "五层瓦楞纸箱", "缓冲泡棉", "缠绕膜", "标签"),
            "SF": ("齿轮坯", "壳体毛坯", "线束组件", "转子冲片", "阀芯组件", "支架焊接件"),
        },
        item_categories={
            "FG": ("FRA", "KNU", "GER", "STA", "VLV", "DRV", "BRK", "ARM"),
            "RM": ("CRS", "ALU", "COP", "PLN", "MAG", "BRG", "SEL", "ELC"),
            "PK": ("PAL", "VCI", "CTN", "FOA", "FIL", "LBL"),
            "SF": ("GBL", "HBL", "WIR", "LAM", "VSP", "WLD"),
        },
        regions=(("江苏省", "苏州市"), ("江苏省", "无锡市"), ("浙江省", "宁波市"), ("浙江省", "杭州市"), ("上海市", "上海市"), ("湖北省", "武汉市"), ("广东省", "广州市"), ("山东省", "青岛市")),
    )


def _fmcg() -> Scenario:
    return Scenario(
        code="fmcg-distribution",
        display_name="臻选快消 · 华东经销网络",
        industry="快消品分销（饮料 / 休闲零食 / 个护 / 调味品）",
        story="上海总部统一管理 30 家一级/二级经销商与 KA 直营系统；月度促销与阶梯返利先计提后核销，"
        "信用额度与账期约束大量小额订单，月末以经销商结算单对账并处理促销费用差异。",
        base_currency="CNY",
        orgs=(
            {"org_code": "ORG-SH", "org_name": "上海总部", "legal_entity": "臻选快消（上海）有限公司", "country": "CN", "base_currency": "CNY"},
            {"org_code": "ORG-HZ", "org_name": "杭州分仓", "legal_entity": "臻选快消（杭州）有限公司", "country": "CN", "base_currency": "CNY"},
            {"org_code": "ORG-NJ", "org_name": "南京分仓", "legal_entity": "臻选快消（南京）有限公司", "country": "CN", "base_currency": "CNY"},
        ),
        warehouses=(
            {"wh_code": "WH-SH-01", "org_code": "ORG-SH", "wh_name": "上海松江中心仓", "wh_type": "成品仓", "province": "上海市", "city": "上海市", "address": "上海市松江区新桥镇新格路 905 号"},
            {"wh_code": "WH-HZ-01", "org_code": "ORG-HZ", "wh_name": "杭州余杭分拨仓", "wh_type": "成品仓", "province": "浙江省", "city": "杭州市", "address": "杭州市余杭区良渚街道通运街 168 号"},
            {"wh_code": "WH-NJ-01", "org_code": "ORG-NJ", "wh_name": "南京江宁分拨仓", "wh_type": "成品仓", "province": "江苏省", "city": "南京市", "address": "南京市江宁区秣陵街道天元中路 128 号"},
        ),
        currencies=({"currency": "CNY", "currency_name": "人民币", "rate_to_cny": "1.000000"},),
        taxes=(
            {"tax_code": "VAT13", "tax_name": "增值税 13%", "country": "CN", "rate": "0.130000"},
            {"tax_code": "VAT9", "tax_name": "增值税 9%（农产品）", "country": "CN", "rate": "0.090000"},
        ),
        customer_kind="fmcg",
        supplier_kinds=("SUG", "JUI", "OIL", "PKG", "OEM"),
        item_counts=(300, 160, 100, 40),
        so_count=700,
        so_line_range=(2, 4),
        po_count=220,
        po_line_range=(2, 5),
        mo_count=90,
        periods=("2026-05", "2026-06", "2026-07", "2026-08", "2026-09"),
        recon_batches=3,
        scenario_extra="fmcg",
        customer_words=(
            "永辉", "大润发", "华润万家", "物美", "联华", "苏果", "步步高", "家家悦", "红旗连锁", "中百仓储",
            "武商", "重百", "天虹", "人人乐", "卜蜂莲花", "盒马", "叮咚买菜", "美团优选", "多多买菜", "兴盛优选",
            "美宜佳", "罗森", "全家", "便利蜂", "见福", "芙蓉兴盛", "好邻居", "唐久", "每一天", "邻几",
        ),
        supplier_words=(
            "中粮糖业", "益海嘉里", "光明乳业", "伊利集团", "蒙牛乳业", "海天味业", "李锦记", "太太乐",
            "安琪酵母", "梅花生物", "紫江企业", "奥瑞金", "中粮包装", "裕同科技", "合兴包装", "永新股份",
            "今麦郎", "统一企业", "康师傅", "娃哈哈", "农夫山泉", "元气森林", "东鹏饮料", "香飘飘",
            "三只松鼠", "良品铺子", "来伊份", "盐津铺子", "洽洽食品", "桃李面包", "安井食品", "三全食品",
            "恒顺醋业", "千禾味业", "中炬高新", "加加食品", "金龙鱼", "鲁花集团", "长寿花", "西王食品",
        ),
        item_words={
            "FG": ("青柠气泡水", "零糖乌龙茶", "每日坚果礼盒", "海盐苏打饼干", "氨基酸洗发水", "原生木浆抽纸", "鲜味生抽", "藤椒油"),
            "RM": ("白砂糖", "浓缩苹果汁", "棕榈油", "高筋面粉", "乳清蛋白", "食用香精", "柠檬酸", "食盐"),
            "PK": ("PET 瓶胚", "热收缩标签", "瓦楞纸箱", "复合膜卷", "礼盒纸托", "封口铝箔"),
            "SF": ("调配液半成品", "饼干面团", "洗发水基料", "酱料熬制半成品", "坚果烘烤半成品", "饮料浓缩液"),
        },
        item_categories={
            "FG": ("SPK", "TEA", "NUT", "BSC", "SHP", "TIS", "SOY", "OIL"),
            "RM": ("SUG", "JUI", "PLM", "FLR", "WHE", "FLA", "CIT", "SLT"),
            "PK": ("PRF", "SHK", "CTN", "FLS", "TRY", "FOL"),
            "SF": ("BLN", "DOU", "BAS", "SAU", "NUT", "CNC"),
        },
        regions=(("上海市", "上海市"), ("浙江省", "杭州市"), ("江苏省", "南京市"), ("江苏省", "苏州市"), ("安徽省", "合肥市"), ("山东省", "济南市"), ("福建省", "厦门市"), ("湖北省", "武汉市")),
    )


def _xb() -> Scenario:
    return Scenario(
        code="cross-border-multicurrency",
        display_name="瀚海跨境 · 多主体多币种结算",
        industry="跨境供应链（消费电子 / 家居 / 户外）",
        story="深圳、香港、新加坡三个主体做转口与本地交付；外币订单按成交日汇率折算，进口报关计提关税与进口增值税，"
        "月末跨主体按内部转移价结算，供应商对账单同时出现汇率、税率与单位换算差异。",
        base_currency="CNY",
        orgs=(
            {"org_code": "ORG-SZ", "org_name": "深圳主体", "legal_entity": "瀚海跨境供应链（深圳）有限公司", "country": "CN", "base_currency": "CNY"},
            {"org_code": "ORG-HK", "org_name": "香港主体", "legal_entity": "瀚海国际贸易（香港）有限公司", "country": "HK", "base_currency": "HKD"},
            {"org_code": "ORG-SG", "org_name": "新加坡主体", "legal_entity": "Hanhai Supply Chain Pte. Ltd.", "country": "SG", "base_currency": "USD"},
        ),
        warehouses=(
            {"wh_code": "WH-SZ-01", "org_code": "ORG-SZ", "wh_name": "深圳前海保税仓", "wh_type": "保税仓", "province": "广东省", "city": "深圳市", "address": "深圳市前海深港合作区听海大道 5059 号 A 库"},
            {"wh_code": "WH-HK-01", "org_code": "ORG-HK", "wh_name": "香港葵涌中转仓", "wh_type": "中转仓", "province": "香港特别行政区", "city": "香港", "address": "香港葵涌货柜码头南路 18 号 3 楼"},
            {"wh_code": "WH-SG-01", "org_code": "ORG-SG", "wh_name": "新加坡樟宜海外仓", "wh_type": "海外仓", "province": "新加坡", "city": "新加坡", "address": "60 Airport Boulevard, Changi, Singapore"},
        ),
        currencies=(
            {"currency": "CNY", "currency_name": "人民币", "rate_to_cny": "1.000000"},
            {"currency": "USD", "currency_name": "美元", "rate_to_cny": "7.118000"},
            {"currency": "EUR", "currency_name": "欧元", "rate_to_cny": "7.742000"},
            {"currency": "HKD", "currency_name": "港币", "rate_to_cny": "0.912000"},
            {"currency": "JPY", "currency_name": "日元", "rate_to_cny": "0.047800"},
        ),
        taxes=(
            {"tax_code": "EXPORT0", "tax_name": "出口零税率", "country": "CN", "rate": "0.000000"},
            {"tax_code": "IMPORT13", "tax_name": "进口增值税 13%", "country": "CN", "rate": "0.130000"},
            {"tax_code": "VAT13", "tax_name": "增值税 13%", "country": "CN", "rate": "0.130000"},
            {"tax_code": "GST9", "tax_name": "新加坡 GST 9%", "country": "SG", "rate": "0.090000"},
        ),
        customer_kind="xb",
        supplier_kinds=("BAT", "CHP", "ALU", "PCB", "PKG", "LOG"),
        item_counts=(280, 180, 90, 50),
        so_count=360,
        so_line_range=(4, 8),
        po_count=180,
        po_line_range=(2, 5),
        mo_count=60,
        periods=("2026-05", "2026-06", "2026-07", "2026-08", "2026-09"),
        recon_batches=3,
        scenario_extra="crossborder",
        customer_words=(
            "北欧极光贸易", "环太平洋采购集团", "中东绿洲分销", "莱茵工业供应", "东京湾商事", "首尔汉江流通",
            "新加坡星洲贸易", "迪拜海湾采购", "北美西岸电子", "澳洲南十字供应链", "法兰克福消费电子", "米兰家居设计",
            "大阪关西商社", "洛杉矶太平洋百货", "温哥华枫叶贸易", "阿姆斯特丹郁金香商贸", "孟买香料之路", "曼谷湄南河贸易",
            "吉隆坡双子塔商贸", "雅加达群岛供应", "圣保罗南美贸易", "墨西哥城太阳石商贸", "约翰内斯堡黄金城", "开普敦好望角贸易",
            "斯德哥尔摩北星采购", "苏黎世阿尔卑斯商贸", "维也纳多瑙河贸易", "布拉格波西米亚供应", "华沙维斯瓦商贸", "里斯本大航海贸易",
        ),
        supplier_words=(
            "宁德时代", "比亚迪电子", "欣旺达", "德赛电池", "亿纬锂能", "鹏鼎控股", "立讯精密", "蓝思科技",
            "伯恩光学", "舜宇光电", "欧菲光", "京东方精电", "天马微电子", "汇顶科技", "韦尔半导体", "兆易创新",
            "士兰微电子", "华润微电子", "长电科技", "通富微电", "中芯集成", "华虹半导体", "闻泰科技", "龙旗科技",
            "华勤技术", "光弘科技", "深科技", "长城开发", "环旭电子", "工业富联", "顺丰国际", "中外运",
            "嘉里物流", "菜鸟国际", "京东国际", "圆通国际", "中通国际", "申通国际", "韵达国际", "极兔国际",
            "裕同科技", "合兴包装", "美盈森", "劲嘉股份", "紫江企业", "奥瑞金", "中粮包装", "永新股份",
        ),
        item_words={
            "FG": ("无线降噪耳机", "便携储能电源", "智能门锁", "户外露营灯", "折叠电动滑板车", "空气净化器", "智能咖啡机", "筋膜枪"),
            "RM": ("锂离子电芯", "主控芯片", "铝合金型材", "PCB 板", "显示屏模组", "连接器", "传感器", "磁性材料"),
            "PK": ("彩盒", "多语言说明书", "EPE 内托", "防静电袋", "外箱", "封箱胶带"),
            "SF": ("主板组件", "结构件组件", "电池包组件", "整机装配半成品", "线材组件", "包装套件"),
        },
        item_categories={
            "FG": ("HPH", "PWR", "LCK", "LMP", "SCT", "PUR", "COF", "MSG"),
            "RM": ("CEL", "IC", "ALU", "PCB", "LCM", "CON", "SEN", "MAG"),
            "PK": ("BOX", "MAN", "EPE", "ESD", "CTN", "TAP"),
            "SF": ("MBA", "STR", "BAT", "ASM", "CAB", "PKG"),
        },
        regions=(("广东省", "深圳市"), ("广东省", "东莞市"), ("香港特别行政区", "香港"), ("新加坡", "新加坡"), ("德国", "法兰克福"), ("美国", "洛杉矶"), ("日本", "大阪"), ("阿联酋", "迪拜")),
    )


SCENARIOS = {s.code: s for s in (_mfg(), _fmcg(), _xb())}


# --------------------------------------------------------------------------- 主数据


def build_master(cfg: Scenario, rng: random.Random) -> dict[str, list[dict]]:
    data: dict[str, list[dict]] = defaultdict(list)
    data["scenario"].append(
        {
            "scenario": cfg.code,
            "display_name": cfg.display_name,
            "industry": cfg.industry,
            "story": cfg.story,
            "base_currency": cfg.base_currency,
            "timezone": "Asia/Shanghai",
            "generated_at": ts(datetime(2026, 11, 11, 9, 0, tzinfo=TZ)),
            "seed": stable_seed(cfg.code),
        }
    )
    for org in cfg.orgs:
        data["organization"].append({"scenario": cfg.code, "timezone": "Asia/Shanghai", **org})
    for wh in cfg.warehouses:
        data["warehouse"].append({"scenario": cfg.code, **wh})

    for cur in cfg.currencies:
        for month in range(12):
            day = WINDOW_START + timedelta(days=30 * month)
            jitter = Decimal(1) + (Decimal(rng.randint(-25, 25)) / Decimal(10000))
            rate = money4(Decimal(cur["rate_to_cny"]) * jitter) if cur["currency"] != "CNY" else Decimal("1.0000")
            data["currency"].append(
                {
                    "scenario": cfg.code,
                    "currency": cur["currency"],
                    "currency_name": cur["currency_name"],
                    "rate_to_cny": str(rate),
                    "rate_date": iso(day),
                }
            )
    for tax in cfg.taxes:
        data["tax_rate"].append({"scenario": cfg.code, "effective_from": iso(WINDOW_START), **tax})

    used_names: set[str] = set()

    def unique(name: str, city: str) -> str:
        if name not in used_names:
            used_names.add(name)
            return name
        alt = f"{name}（{city}）"
        n = 2
        while alt in used_names:
            alt = f"{name}（{city}{n}）"
            n += 1
        used_names.add(alt)
        return alt

    for i in range(30):
        province, city = cfg.regions[i % len(cfg.regions)]
        word = cfg.customer_words[i % len(cfg.customer_words)]
        if cfg.customer_kind == "mfg":
            suffix = rng.choice(("汽车零部件有限公司", "精密制造有限公司", "装备制造有限公司", "供应链管理有限公司"))
            channel = rng.choice(("主机厂直供", "一级配套", "售后渠道"))
            tier = rng.choice(("战略客户", "重点客户", "普通客户"))
        elif cfg.customer_kind == "fmcg":
            suffix = rng.choice(("商贸有限公司", "连锁超市有限公司", "供应链管理有限公司", "便利店管理有限公司"))
            channel = rng.choice(("现代渠道", "传统渠道", "电商渠道", "便利店"))
            tier = TIER1 if i < 12 else (TIER2 if i < 24 else KA)
        else:
            suffix = rng.choice(("国际贸易有限公司", "采购集团（亚太）", "供应链有限公司", "商贸株式会社"))
            channel = rng.choice(("转口贸易", "本地分销", "跨境电商", "项目采购"))
            tier = rng.choice(("A 类客户", "B 类客户", "C 类客户"))
        name = unique(f"{city}{word}{suffix}", city)
        org = cfg.orgs[i % len(cfg.orgs)]
        currency = cfg.base_currency
        if cfg.customer_kind == "xb":
            currency = ("USD", "EUR", "HKD", "JPY")[i % 4]
        terms = rng.choice((30, 45, 60, 90))
        limit = money(rng.choice((200000, 500000, 800000, 1500000, 3000000, 5000000)))
        data["customer"].append(
            {
                "scenario": cfg.code,
                "customer_code": f"C-{('AUTO' if cfg.customer_kind == 'mfg' else 'DIST' if cfg.customer_kind == 'fmcg' else 'OVR')}-{i + 1:04d}",
                "customer_name": name,
                "customer_group": word + "集团",
                "parent_code": "",
                "tier": tier,
                "channel": channel,
                "org_code": org["org_code"],
                "currency": currency,
                "payment_terms": terms,
                "credit_limit": str(limit),
                "credit_used": str(money(limit * Decimal(rng.randint(15, 85)) / Decimal(100))),
                "tax_id": f"9132{rng.randint(100000, 999999)}MA{rng.randint(1000, 9999)}X",
                "province": province,
                "city": city,
                "address": f"{province}{city}{rng.choice(('工业园区', '高新区', '经济开发区', '临港产业园'))}{rng.randint(1, 999)}号{rng.randint(1, 12)}幢",
                "sales_rep": rng.choice(("周瑾", "许嘉禾", "林澈", "程斯远", "叶知秋", "韩沐", "乔言", "温以宁")),
                "status": "合作中" if rng.random() > 0.06 else "暂停合作",
            }
        )

    # 快消：二级经销商挂到一级经销商
    if cfg.customer_kind == "fmcg":
        tier1 = [row["customer_code"] for row in data["customer"] if row["tier"] == TIER1]
        for row in data["customer"]:
            if row["tier"] == TIER2:
                row["parent_code"] = tier1[rng.randrange(len(tier1))]

    supplier_names: set[str] = set()
    for i in range(80):
        province, city = cfg.regions[(i * 3) % len(cfg.regions)]
        word = cfg.supplier_words[i % len(cfg.supplier_words)]
        kind = cfg.supplier_kinds[i % len(cfg.supplier_kinds)]
        suffix = rng.choice(("股份有限公司", "集团有限公司", "供应链有限公司", "制造有限公司"))
        name = f"{word}{suffix}"
        if name in supplier_names:
            name = f"{word}（{city}）{suffix}"
            n = 2
            while name in supplier_names:
                name = f"{word}（{city}{n}）{suffix}"
                n += 1
        supplier_names.add(name)
        grade = "A" if i < 20 else ("B" if i < 55 else "C")
        org = cfg.orgs[i % len(cfg.orgs)]
        data["supplier"].append(
            {
                "scenario": cfg.code,
                "supplier_code": f"S-{kind}-{i + 1:04d}",
                "supplier_name": name,
                "category": {
                    "STL": "钢材", "ALU": "铝材", "ELE": "电子元件", "PKG": "包装材料", "CHE": "化工原料",
                    "SUG": "糖类原料", "JUI": "果汁原料", "OIL": "油脂原料", "OEM": "代工生产",
                    "BAT": "电池模组", "CHP": "芯片半导体", "PCB": "电路板", "LOG": "国际物流",
                }.get(kind, kind),
                "grade": grade,
                "org_code": org["org_code"],
                "currency": cfg.base_currency if cfg.customer_kind != "xb" else ("USD" if i % 3 else "CNY"),
                "payment_terms": rng.choice((30, 45, 60, 90)),
                "country": "CN" if cfg.customer_kind != "xb" or i % 3 else "HK",
                "contact_name": rng.choice(("陈默", "高远", "邵宁", "唐棠", "白露", "秦川", "苏禾", "罗一鸣")),
                "phone": f"1{rng.choice('3578')}{rng.randint(100000000, 999999999)}",
                "tax_id": f"9131{rng.randint(100000, 999999)}MA{rng.randint(1000, 9999)}Y",
                "province": province,
                "city": city,
                "address": f"{province}{city}{rng.choice(('工业园', '产业园', '物流园', '科技园'))}{rng.randint(1, 88)}路{rng.randint(1, 200)}号",
                "on_time_rate": str(money(rng.uniform(86, 99.8))),
                "quality_rate": str(money(rng.uniform(94, 99.9))),
                "status": "合格" if grade != "C" else "观察",
            }
        )

    fg_n, rm_n, pk_n, sf_n = cfg.item_counts
    type_plan = (("FG", fg_n), ("RM", rm_n), ("PK", pk_n), ("SF", sf_n))
    seq_by_type = defaultdict(int)
    for item_type, count in type_plan:
        words = cfg.item_words[item_type]
        for i in range(count):
            seq_by_type[item_type] += 1
            word = words[i % len(words)]
            if item_type == "FG":
                spec = f"{rng.choice(('A', 'B', 'C', 'E', 'S'))}{rng.randint(1, 9)}-{rng.randint(10, 99)}"
                std_cost = money(rng.uniform(35, 620))
                list_price = money(std_cost * Decimal(rng.uniform(1.25, 1.75)))
                shelf = rng.choice((0, 180, 365, 730))
                tax = "VAT13"
            elif item_type == "RM":
                spec = f"{rng.choice(('Q235', '6061', '304', 'PA66', 'N52'))}-{rng.randint(0, 9)}.{rng.randint(1, 9)}"
                std_cost = money(rng.uniform(5, 260))
                list_price = std_cost
                shelf = rng.choice((0, 90, 180, 365))
                tax = "VAT13"
            elif item_type == "PK":
                spec = f"{rng.randint(200, 1200)}×{rng.randint(150, 900)}×{rng.randint(50, 400)}mm"
                std_cost = money(rng.uniform(0.8, 22))
                list_price = std_cost
                shelf = 0
                tax = "VAT13"
            else:
                spec = f"{rng.choice(('焊接件', '机加工件', '注塑件', '冲压件'))}-{rng.randint(100, 999)}"
                std_cost = money(rng.uniform(18, 320))
                list_price = std_cost
                shelf = rng.choice((0, 90, 180))
                tax = "VAT13"
            cat = words[i % len(words)][:2]
            cat_code = cfg.item_categories[item_type][i % len(cfg.item_categories[item_type])]
            data["item"].append(
                {
                    "scenario": cfg.code,
                    "item_code": f"{item_type}-{cat_code}-{seq_by_type[item_type]:04d}",
                    "item_name": f"{word} {spec}",
                    "item_type": item_type,
                    "category": cat,
                    "uom": "PCS" if item_type in ("FG", "SF", "PK") else rng.choice(("KG", "M", "PCS")),
                    "spec": spec,
                    "std_cost": str(std_cost),
                    "list_price": str(list_price),
                    "tax_code": tax,
                    "shelf_life_days": shelf,
                    "abc_class": "A" if i < count * 0.2 else ("B" if i < count * 0.55 else "C"),
                    "status": "启用",
                }
            )
    return data


def build_bom(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    fg = [row for row in data["item"] if row["item_type"] == "FG"]
    sf = [row for row in data["item"] if row["item_type"] == "SF"]
    rm = [row for row in data["item"] if row["item_type"] == "RM"]
    pk = [row for row in data["item"] if row["item_type"] == "PK"]
    for parent in fg:
        children = rng.sample(sf, k=min(len(sf), rng.randint(1, 2))) + rng.sample(rm, k=rng.randint(1, 3))
        for child in children:
            data["bom_line"].append(
                {
                    "scenario": cfg.code,
                    "bom_code": f"BOM-{parent['item_code']}",
                    "parent_item": parent["item_code"],
                    "child_item": child["item_code"],
                    "level": 1,
                    "qty_per": str(money4(rng.uniform(0.2, 4.5))),
                    "scrap_rate": str(rate4(rng.uniform(0.005, 0.045))),
                    "uom": child["uom"],
                    "effective_from": iso(daterange(rng, WINDOW_START, TODAY - timedelta(days=120))),
                }
            )
        for pkg in rng.sample(pk, k=min(len(pk), 1)):
            data["bom_line"].append(
                {
                    "scenario": cfg.code,
                    "bom_code": f"BOM-{parent['item_code']}",
                    "parent_item": parent["item_code"],
                    "child_item": pkg["item_code"],
                    "level": 1,
                    "qty_per": str(money4(rng.uniform(0.02, 0.2))),
                    "scrap_rate": "0.0100",
                    "uom": pkg["uom"],
                    "effective_from": iso(daterange(rng, WINDOW_START, TODAY - timedelta(days=120))),
                }
            )
    for parent in sf:
        for child in rng.sample(rm, k=min(len(rm), rng.randint(2, 3))):
            data["bom_line"].append(
                {
                    "scenario": cfg.code,
                    "bom_code": f"BOM-{parent['item_code']}",
                    "parent_item": parent["item_code"],
                    "child_item": child["item_code"],
                    "level": 2,
                    "qty_per": str(money4(rng.uniform(0.1, 2.8))),
                    "scrap_rate": str(rate4(rng.uniform(0.005, 0.06))),
                    "uom": child["uom"],
                    "effective_from": iso(daterange(rng, WINDOW_START, TODAY - timedelta(days=150))),
                }
            )


# --------------------------------------------------------------------------- 交易


def _line_prices(cfg: Scenario, item: dict, customer_currency: str, rng: random.Random) -> tuple[Decimal, Decimal]:
    """返回 (本币单价, 订单币种单价)。外币按固定演示汇率折算。"""
    base = Decimal(item["list_price"])
    price_cny = money(base * Decimal(rng.uniform(0.94, 1.06)))
    if customer_currency == "CNY":
        return price_cny, price_cny
    fx = {"USD": Decimal("7.118"), "EUR": Decimal("7.742"), "HKD": Decimal("0.912"), "JPY": Decimal("0.0478")}[customer_currency]
    return price_cny, money4(price_cny / fx)


def build_sales(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    customers = data["customer"]
    items = data["item"]
    fg = [row for row in items if row["item_type"] == "FG"]
    sf = [row for row in items if row["item_type"] == "SF"]
    sellable = fg + sf if cfg.customer_kind == "mfg" else fg
    tail_date = TODAY - timedelta(days=45)
    for idx in range(cfg.so_count):
        customer = customers[rng.randrange(len(customers))]
        org = next(o for o in cfg.orgs if o["org_code"] == customer["org_code"])
        wh = next(w for w in cfg.warehouses if w["org_code"] == org["org_code"])
        order_date = daterange(rng, WINDOW_START, tail_date)
        promised = cap_today(order_date + timedelta(days=rng.randint(3, 21)))
        currency = customer["currency"]
        line_count = rng.randint(*cfg.so_line_range)
        so_no = f"SO-{order_date.strftime('%Y%m')}-{idx + 1:05d}"
        lines: list[dict] = []
        subtotal = Decimal("0")
        tax_total = Decimal("0")
        chosen = rng.sample(sellable, k=min(len(sellable), line_count))
        for line_no, item in enumerate(chosen, start=1):
            qty = rng.randint(1, 40 if cfg.customer_kind == "fmcg" else 240)
            price_cny, price = _line_prices(cfg, item, currency, rng)
            discount = Decimal("0") if cfg.customer_kind == "mfg" else Decimal(rng.choice(("0", "0.02", "0.05", "0.08")))
            amount = money(Decimal(qty) * price * (Decimal(1) - discount))
            tax_rate = Decimal("0.13") if cfg.customer_kind != "xb" else Decimal("0")
            tax_amount = money(amount * tax_rate)
            subtotal += amount
            tax_total += tax_amount
            lines.append(
                {
                    "scenario": cfg.code,
                    "so_no": so_no,
                    "line_no": line_no,
                    "item_code": item["item_code"],
                    "qty": str(money4(qty)),
                    "uom": item["uom"],
                    "unit_price": str(price),
                    "discount_rate": str(discount),
                    "tax_code": "VAT13" if cfg.customer_kind != "xb" else "EXPORT0",
                    "amount": str(amount),
                    "tax_amount": str(tax_amount),
                    "warehouse_code": wh["wh_code"],
                    "_unit_cny": price_cny,
                }
            )
        status = "已完成" if order_date < TODAY - timedelta(days=90) else rng.choice(("已发货", "部分发货", "已开票"))
        data["sales_order"].append(
            {
                "scenario": cfg.code,
                "so_no": so_no,
                "customer_code": customer["customer_code"],
                "org_code": org["org_code"],
                "order_date": iso(order_date),
                "promised_date": iso(promised),
                "currency": currency,
                "payment_terms": customer["payment_terms"],
                "subtotal": str(money(subtotal)),
                "tax_amount": str(money(tax_total)),
                "total_amount": str(money(subtotal + tax_total)),
                "status": status,
                "sales_rep": customer["sales_rep"],
            }
        )
        data["sales_order_line"].extend([{k: v for k, v in line.items() if k != "_unit_cny"} for line in lines])

        # 发货：85% 整单发货，15% 部分发货
        partial = rng.random() < 0.15
        delivery_date = order_date + timedelta(days=rng.randint(1, 12))
        if delivery_date > TODAY - timedelta(days=20):
            delivery_date = TODAY - timedelta(days=20)
        delivery_no = f"DN-{delivery_date.strftime('%Y%m')}-{idx + 1:05d}"
        data["sales_delivery"].append(
            {
                "scenario": cfg.code,
                "delivery_no": delivery_no,
                "so_no": so_no,
                "customer_code": customer["customer_code"],
                "warehouse_code": wh["wh_code"],
                "delivery_date": iso(delivery_date),
                "carrier": rng.choice(("顺丰速运", "德邦物流", "京东物流", "中远海运", "DHL 国际")),
                "tracking_no": f"SF{rng.randint(10**11, 10**12 - 1)}",
                "status": "已签收",
            }
        )
        delivery_lines: list[dict] = []
        for line_no, line in enumerate(lines, start=1):
            order_qty = Decimal(line["qty"])
            shipped = order_qty if not partial else money4(order_qty * Decimal(rng.randint(55, 90)) / Decimal(100))
            lot_no = f"LOT-{line['item_code'].split('-')[-1]}-{delivery_date.strftime('%y%m%d')}-{idx + 1:05d}-{line_no:02d}"
            delivery_lines.append(
                {
                    "scenario": cfg.code,
                    "delivery_no": delivery_no,
                    "line_no": line_no,
                    "so_no": so_no,
                    "so_line_no": line_no,
                    "item_code": line["item_code"],
                    "qty": str(shipped),
                    "lot_no": lot_no,
                }
            )
        data["sales_delivery_line"].extend(delivery_lines)

        invoice_date = delivery_date + timedelta(days=rng.randint(1, 6))
        if invoice_date > TODAY - timedelta(days=10):
            invoice_date = TODAY - timedelta(days=10)
        due_date = cap_today(invoice_date + timedelta(days=customer["payment_terms"]))
        invoice_no = f"SI-{invoice_date.strftime('%Y%m')}-{idx + 1:05d}"
        fx_rate = Decimal("1") if currency == "CNY" else Decimal(
            {"USD": "7.118", "EUR": "7.742", "HKD": "0.912", "JPY": "0.0478"}[currency]
        )
        inv_subtotal = Decimal("0")
        inv_tax = Decimal("0")
        for line_no, line in enumerate(lines, start=1):
            qty = Decimal(delivery_lines[line_no - 1]["qty"])
            unit_price = Decimal(line["unit_price"])
            discount = Decimal(line["discount_rate"])
            amount = money(qty * unit_price * (Decimal(1) - discount))
            tax_amount = money(amount * (Decimal("0.13") if cfg.customer_kind != "xb" else Decimal("0")))
            inv_subtotal += amount
            inv_tax += tax_amount
            data["sales_invoice_line"].append(
                {
                    "scenario": cfg.code,
                    "invoice_no": invoice_no,
                    "line_no": line_no,
                    "delivery_no": delivery_no,
                    "delivery_line_no": line_no,
                    "item_code": line["item_code"],
                    "qty": str(qty),
                    "unit_price": str(unit_price),
                    "tax_code": line["tax_code"],
                    "amount": str(amount),
                    "tax_amount": str(tax_amount),
                }
            )
        total = money(inv_subtotal + inv_tax)
        data["sales_invoice"].append(
            {
                "scenario": cfg.code,
                "invoice_no": invoice_no,
                "customer_code": customer["customer_code"],
                "delivery_no": delivery_no,
                "org_code": org["org_code"],
                "invoice_date": iso(invoice_date),
                "due_date": iso(due_date),
                "currency": currency,
                "fx_rate": str(fx_rate),
                "subtotal": str(money(inv_subtotal)),
                "tax_amount": str(money(inv_tax)),
                "total_amount": str(total),
                "total_cny": str(money(total * fx_rate)),
                "status": "已开票" if due_date > TODAY else "已收款",
            }
        )


def build_purchase(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    suppliers = data["supplier"]
    items = [row for row in data["item"] if row["item_type"] in ("RM", "PK", "SF")]
    tail_date = TODAY - timedelta(days=55)
    for idx in range(cfg.po_count):
        supplier = suppliers[rng.randrange(len(suppliers))]
        org = next(o for o in cfg.orgs if o["org_code"] == supplier["org_code"])
        wh = next(w for w in cfg.warehouses if w["org_code"] == org["org_code"])
        order_date = daterange(rng, WINDOW_START, tail_date)
        expected = cap_today(order_date + timedelta(days=rng.randint(5, 30)))
        currency = supplier["currency"]
        po_no = f"PO-{order_date.strftime('%Y%m')}-{idx + 1:05d}"
        lines: list[dict] = []
        subtotal = Decimal("0")
        tax_total = Decimal("0")
        for line_no, item in enumerate(rng.sample(items, k=rng.randint(*cfg.po_line_range)), start=1):
            qty = rng.randint(20, 2000 if item["item_type"] == "RM" else 800)
            price_cny = money(Decimal(item["std_cost"]) * Decimal(rng.uniform(0.96, 1.04)))
            if currency == "CNY":
                price = price_cny
            else:
                fx = {"USD": Decimal("7.118"), "HKD": Decimal("0.912")}.get(currency, Decimal("1"))
                price = money4(price_cny / fx)
            amount = money(Decimal(qty) * price)
            tax_amount = money(amount * Decimal("0.13"))
            subtotal += amount
            tax_total += tax_amount
            lines.append(
                {
                    "scenario": cfg.code,
                    "po_no": po_no,
                    "line_no": line_no,
                    "item_code": item["item_code"],
                    "qty": str(money4(qty)),
                    "uom": item["uom"],
                    "unit_price": str(price),
                    "tax_code": "IMPORT13" if cfg.customer_kind == "xb" and currency != "CNY" else "VAT13",
                    "amount": str(amount),
                    "tax_amount": str(tax_amount),
                }
            )
        data["purchase_order"].append(
            {
                "scenario": cfg.code,
                "po_no": po_no,
                "supplier_code": supplier["supplier_code"],
                "org_code": org["org_code"],
                "order_date": iso(order_date),
                "expected_date": iso(expected),
                "currency": currency,
                "payment_terms": supplier["payment_terms"],
                "subtotal": str(money(subtotal)),
                "tax_amount": str(money(tax_total)),
                "total_amount": str(money(subtotal + tax_total)),
                "status": rng.choice(("已入库", "部分入库", "已关闭")),
                "buyer": rng.choice(("方启", "邓嘉", "薛宁", "蒋川", "傅晓")),
            }
        )
        data["purchase_order_line"].extend(lines)

        receipt_date = order_date + timedelta(days=rng.randint(4, 26))
        if receipt_date > TODAY - timedelta(days=25):
            receipt_date = TODAY - timedelta(days=25)
        receipt_no = f"GR-{receipt_date.strftime('%Y%m')}-{idx + 1:05d}"
        partial = rng.random() < 0.2
        data["goods_receipt"].append(
            {
                "scenario": cfg.code,
                "receipt_no": receipt_no,
                "po_no": po_no,
                "supplier_code": supplier["supplier_code"],
                "warehouse_code": wh["wh_code"],
                "receipt_date": iso(receipt_date),
                "inspector": rng.choice(("邱竹", "范舟", "尹禾", "崔灿")),
                "status": "部分收货" if partial else "全部收货",
            }
        )
        receipt_lines: list[dict] = []
        for line_no, line in enumerate(lines, start=1):
            ordered = Decimal(line["qty"])
            received = ordered if not partial else money4(ordered * Decimal(rng.randint(60, 95)) / Decimal(100))
            rejected = money4(received * Decimal(rng.choice(("0", "0", "0", "0.005", "0.01"))))
            lot_no = f"LOT-{line['item_code'].split('-')[-1]}-{receipt_date.strftime('%y%m%d')}-{idx + 1:05d}-{line_no:02d}"
            receipt_lines.append(
                {
                    "scenario": cfg.code,
                    "receipt_no": receipt_no,
                    "line_no": line_no,
                    "po_no": po_no,
                    "po_line_no": line_no,
                    "item_code": line["item_code"],
                    "qty_received": str(received),
                    "qty_rejected": str(rejected),
                    "lot_no": lot_no,
                }
            )
            item = next(row for row in items if row["item_code"] == line["item_code"])
            shelf = int(item["shelf_life_days"] or 0)
            production_date = receipt_date - timedelta(days=rng.randint(1, 20))
            expiry = production_date + timedelta(days=shelf) if shelf else None
            data["inventory_lot"].append(
                {
                    "scenario": cfg.code,
                    "lot_no": lot_no,
                    "item_code": line["item_code"],
                    "warehouse_code": wh["wh_code"],
                    "org_code": org["org_code"],
                    "receipt_no": receipt_no,
                    "qty_received": str(received),
                    "qty_on_hand": str(money4(received - rejected - money4(received * Decimal(rng.randint(0, 60)) / Decimal(100)))),
                    "unit_cost": line["unit_price"],
                    "production_date": iso(production_date),
                    "expiry_date": iso(expiry) if expiry else "",
                    "age_days": (TODAY - production_date).days,
                    "quality_status": rng.choice(("合格", "合格", "合格", "待检", "冻结")),
                }
            )
        data["goods_receipt_line"].extend(receipt_lines)

        invoice_date = receipt_date + timedelta(days=rng.randint(1, 10))
        if invoice_date > TODAY - timedelta(days=15):
            invoice_date = TODAY - timedelta(days=15)
        due_date = cap_today(invoice_date + timedelta(days=supplier["payment_terms"]))
        invoice_no = f"PI-{invoice_date.strftime('%Y%m')}-{idx + 1:05d}"
        fx_rate = Decimal("1") if currency == "CNY" else Decimal({"USD": "7.118", "HKD": "0.912"}.get(currency, "1"))
        inv_subtotal = Decimal("0")
        inv_tax = Decimal("0")
        for line_no, line in enumerate(lines, start=1):
            qty = Decimal(receipt_lines[line_no - 1]["qty_received"]) - Decimal(receipt_lines[line_no - 1]["qty_rejected"])
            unit_price = Decimal(line["unit_price"])
            amount = money(qty * unit_price)
            tax_amount = money(amount * Decimal("0.13"))
            inv_subtotal += amount
            inv_tax += tax_amount
            data["purchase_invoice_line"].append(
                {
                    "scenario": cfg.code,
                    "invoice_no": invoice_no,
                    "line_no": line_no,
                    "receipt_no": receipt_no,
                    "receipt_line_no": line_no,
                    "item_code": line["item_code"],
                    "qty": str(money4(qty)),
                    "unit_price": str(unit_price),
                    "tax_code": line["tax_code"],
                    "amount": str(amount),
                    "tax_amount": str(tax_amount),
                }
            )
        total = money(inv_subtotal + inv_tax)
        data["purchase_invoice"].append(
            {
                "scenario": cfg.code,
                "invoice_no": invoice_no,
                "supplier_code": supplier["supplier_code"],
                "receipt_no": receipt_no,
                "org_code": org["org_code"],
                "invoice_date": iso(invoice_date),
                "due_date": iso(due_date),
                "currency": currency,
                "fx_rate": str(fx_rate),
                "subtotal": str(money(inv_subtotal)),
                "tax_amount": str(money(inv_tax)),
                "total_amount": str(total),
                "total_cny": str(money(total * fx_rate)),
                "status": "已入账" if due_date > TODAY else "已付款",
            }
        )


def build_production(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    fg = [row for row in data["item"] if row["item_type"] == "FG"]
    bom_by_parent: dict[str, list[dict]] = defaultdict(list)
    for line in data["bom_line"]:
        bom_by_parent[line["parent_item"]].append(line)
    for idx in range(cfg.mo_count):
        parent = fg[rng.randrange(len(fg))]
        org = cfg.orgs[rng.randrange(len(cfg.orgs))]
        wh = next(w for w in cfg.warehouses if w["org_code"] == org["org_code"])
        planned = rng.randint(60, 1800)
        start = daterange(rng, WINDOW_START, TODAY - timedelta(days=25))
        due = start + timedelta(days=rng.randint(3, 20))
        status = "已完工" if due < TODAY - timedelta(days=30) else rng.choice(("生产中", "已下达", "已入库"))
        completed = planned if status in ("已完工", "已入库") else rng.randint(int(planned * 0.1), int(planned * 0.9))
        mo_no = f"MO-{start.strftime('%Y%m')}-{idx + 1:05d}"
        data["production_order"].append(
            {
                "scenario": cfg.code,
                "mo_no": mo_no,
                "org_code": org["org_code"],
                "item_code": parent["item_code"],
                "warehouse_code": wh["wh_code"],
                "planned_qty": str(money4(planned)),
                "completed_qty": str(money4(completed)),
                "start_date": iso(start),
                "due_date": iso(due),
                "status": status,
                "production_line": f"{rng.choice(('A', 'B', 'C'))}线",
                "shift": rng.choice(("白班", "夜班", "两班倒")),
            }
        )
        children = bom_by_parent.get(parent["item_code"], [])
        for line_no, child in enumerate(children[:4], start=1):
            required = money4(Decimal(planned) * Decimal(child["qty_per"]) * (Decimal(1) + Decimal(child["scrap_rate"])))
            issued = required if status in ("已完工", "已入库") else money4(required * Decimal(rng.randint(40, 95)) / Decimal(100))
            data["production_material_issue"].append(
                {
                    "scenario": cfg.code,
                    "mo_no": mo_no,
                    "line_no": line_no,
                    "item_code": child["child_item"],
                    "required_qty": str(required),
                    "issued_qty": str(issued),
                    "lot_no": f"LOT-{child['child_item'].split('-')[-1]}-{start.strftime('%y%m%d')}-{line_no:02d}",
                    "issue_date": iso(start),
                }
            )


def build_gl(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    for invoice in data["sales_invoice"]:
        voucher = f"GL-SI-{invoice['invoice_no'][3:]}"
        total = Decimal(invoice["total_amount"])
        subtotal = Decimal(invoice["subtotal"])
        tax = Decimal(invoice["tax_amount"])
        rows = [
            ("1122", "应收账款", total, Decimal("0"), f"销售开票 {invoice['invoice_no']}"),
            ("6001", "主营业务收入", Decimal("0"), subtotal, f"销售开票 {invoice['invoice_no']}"),
        ]
        if tax > 0:
            rows.append(("2221", "应交税费-销项税额", Decimal("0"), tax, f"销项税 {invoice['invoice_no']}"))
        for line_no, (code, name, debit, credit, memo) in enumerate(rows, start=1):
            data["gl_entry"].append(
                {
                    "scenario": cfg.code,
                    "voucher_no": voucher,
                    "line_no": line_no,
                    "posting_date": invoice["invoice_date"],
                    "account_code": code,
                    "account_name": name,
                    "doc_type": "销售发票",
                    "doc_no": invoice["invoice_no"],
                    "currency": invoice["currency"],
                    "debit": str(money(debit)),
                    "credit": str(money(credit)),
                    "memo": memo,
                }
            )
    for invoice in data["purchase_invoice"]:
        voucher = f"GL-PI-{invoice['invoice_no'][3:]}"
        total = Decimal(invoice["total_amount"])
        subtotal = Decimal(invoice["subtotal"])
        tax = Decimal(invoice["tax_amount"])
        rows = [
            ("1405", "库存商品/原材料", subtotal, Decimal("0"), f"采购入账 {invoice['invoice_no']}"),
            ("2221", "应交税费-进项税额", tax, Decimal("0"), f"进项税 {invoice['invoice_no']}"),
            ("2202", "应付账款", Decimal("0"), total, f"采购入账 {invoice['invoice_no']}"),
        ]
        for line_no, (code, name, debit, credit, memo) in enumerate(rows, start=1):
            data["gl_entry"].append(
                {
                    "scenario": cfg.code,
                    "voucher_no": voucher,
                    "line_no": line_no,
                    "posting_date": invoice["invoice_date"],
                    "account_code": code,
                    "account_name": name,
                    "doc_type": "采购发票",
                    "doc_no": invoice["invoice_no"],
                    "currency": invoice["currency"],
                    "debit": str(money(debit)),
                    "credit": str(money(credit)),
                    "memo": memo,
                }
            )


def build_fmcg_extra(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    promo_names = ("夏日冰爽满减", "中秋礼盒搭赠", "开学季组合装", "国庆黄金周陈列", "双十一预售返利", "年货节堆头", "新客首单立减", "渠道专供装")
    for i in range(24):
        start = daterange(rng, WINDOW_START, TODAY - timedelta(days=45))
        end = min(start + timedelta(days=rng.randint(10, 45)), TODAY - timedelta(days=5))
        budget = money(rng.choice((50000, 120000, 200000, 350000)))
        actual = money(budget * Decimal(rng.randint(45, 105)) / Decimal(100))
        data["promotion"].append(
            {
                "scenario": cfg.code,
                "promotion_code": f"PM-{start.strftime('%Y%m')}-{i + 1:03d}",
                "promotion_name": f"{promo_names[i % len(promo_names)]}·{start.strftime('%m')}月",
                "channel": rng.choice(("现代渠道", "传统渠道", "电商渠道", "便利店")),
                "promo_type": rng.choice(("满减", "搭赠", "陈列费", "阶梯返利", "新客补贴")),
                "start_date": iso(start),
                "end_date": iso(end),
                "budget_amount": str(budget),
                "actual_amount": str(actual),
                "status": "已结案" if end < TODAY - timedelta(days=30) else rng.choice(("执行中", "待核销")),
                "owner": rng.choice(("周瑾", "林澈", "叶知秋", "韩沐")),
            }
        )
    for i, customer in enumerate([row for row in data["customer"] if row["tier"] != KA][:18]):
        for p in range(2):
            period_date = TODAY - timedelta(days=30 * (p + 1))
            base = money(rng.randint(300000, 2600000))
            rebate_rate = Decimal(rng.choice(("0.015", "0.02", "0.025", "0.03", "0.04")))
            accrued = money(base * rebate_rate)
            paid = accrued if p == 1 else money(accrued * Decimal(rng.randint(0, 80)) / Decimal(100))
            data["rebate_accrual"].append(
                {
                    "scenario": cfg.code,
                    "accrual_no": f"RB-{period_date.strftime('%Y%m')}-{i + 1:03d}-{p + 1}",
                    "customer_code": customer["customer_code"],
                    "period": period_date.strftime("%Y-%m"),
                    "tier": customer["tier"],
                    "rebate_rate": str(rebate_rate),
                    "sales_base": str(base),
                    "accrued_amount": str(accrued),
                    "paid_amount": str(paid),
                    "status": "已兑付" if paid >= accrued else ("部分兑付" if paid > 0 else "已计提"),
                    "settlement_date": iso(period_date + timedelta(days=20)),
                }
            )


def build_xb_extra(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    for i in range(120):
        receipt = data["goods_receipt"][rng.randrange(len(data["goods_receipt"]))]
        po = next(row for row in data["purchase_order"] if row["po_no"] == receipt["po_no"])
        declared = money(Decimal(po["subtotal"]) * Decimal(rng.randint(92, 100)) / Decimal(100))
        duty_rate = Decimal(rng.choice(("0.05", "0.08", "0.10", "0.12")))
        duty = money(declared * duty_rate)
        vat = money((declared + duty) * Decimal("0.13"))
        data["customs_declaration"].append(
            {
                "scenario": cfg.code,
                "declaration_no": f"CD-{receipt['receipt_date'].replace('-', '')}-{i + 1:04d}",
                "receipt_no": receipt["receipt_no"],
                "supplier_code": receipt["supplier_code"],
                "org_code": po["org_code"],
                "declaration_date": receipt["receipt_date"],
                "port": rng.choice(("深圳前海", "香港葵涌", "上海洋山", "广州南沙", "新加坡樟宜")),
                "hs_code": f"{rng.randint(8471, 8544)}{rng.randint(10, 99)}",
                "declared_value": str(declared),
                "currency": po["currency"],
                "duty_rate": str(duty_rate),
                "duty_amount": str(duty),
                "vat_amount": str(vat),
                "broker": rng.choice(("中外运报关", "嘉里报关", "顺丰国际报关", "东方物流报关")),
                "status": rng.choice(("已放行", "已放行", "查验中", "已转关")),
            }
        )
    for i in range(60):
        period_date = TODAY - timedelta(days=30 * (i % 6 + 1))
        amount = money(rng.randint(80000, 1200000))
        currency = ("USD", "HKD", "EUR")[i % 3]
        fx = Decimal({"USD": "7.118", "HKD": "0.912", "EUR": "7.742"}[currency])
        data["intercompany_settlement"].append(
            {
                "scenario": cfg.code,
                "settlement_no": f"IC-{period_date.strftime('%Y%m')}-{i + 1:03d}",
                "from_org": rng.choice(("ORG-SZ", "ORG-HK", "ORG-SG")),
                "to_org": rng.choice(("ORG-SZ", "ORG-HK", "ORG-SG")),
                "period": period_date.strftime("%Y-%m"),
                "currency": currency,
                "amount": str(amount),
                "fx_rate": str(fx),
                "amount_cny": str(money(amount * fx)),
                "settlement_date": iso(period_date + timedelta(days=25)),
                "status": rng.choice(("已结算", "待结算", "已对账")),
                "note": rng.choice(("内部转移价结算", "代垫运费结算", "跨境服务费分摊", "样品调拨结算")),
            }
        )


def build_fx_rate(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    pairs = (("USD/CNY", "7.118"), ("EUR/CNY", "7.742"), ("HKD/CNY", "0.912"), ("JPY/CNY", "0.0478"))
    for month in range(12):
        day = WINDOW_START + timedelta(days=30 * month)
        for pair, base in pairs:
            jitter = Decimal(1) + Decimal(rng.randint(-40, 40)) / Decimal(10000)
            data["fx_rate"].append(
                {
                    "scenario": cfg.code,
                    "rate_date": iso(day),
                    "pair": pair,
                    "rate": str(money4(Decimal(base) * jitter)),
                    "source": rng.choice(("中国外汇交易中心", "香港金管局", "新加坡金管局")),
                }
            )


# --------------------------------------------------------------------------- 对账案例


STATEMENT_STATUS = {
    "matched": ("已匹配", False, "exact"),
    "unit_price": ("待人工", True, "fuzzy"),
    "qty": ("待人工", True, "fuzzy"),
    "scrap": ("待人工", True, "fuzzy"),
    "uom": ("匹配歧义", True, "candidate"),
    "tax": ("待人工", True, "fuzzy"),
    "missing_doc": ("供应商未匹配", False, "unmatched"),
    "duplicate": ("重复计费", False, "exact"),
    "rounding_tolerance": ("已匹配", False, "exact"),
    "candidate": ("候选待人工", True, "candidate"),
    "unparseable": ("不可解析", True, "unmatched"),
    "our_missing": ("我方未匹配", False, "unmatched"),
}

#: 默认 scope 只看「最新期间」，因此最新期间铺 12 条（前端 10 条/页，可翻两页）；
#: 另外两条放在前两个期间，用于演示切换期间后列表仍有内容。
RECON_BATCH_PROFILES = (
    # 最新期间 12 条：前端每页 10 条，必须能翻到第二页。
    ("clean", 0),
    ("clean", 0),
    ("showcase", 0),
    ("showcase", 0),
    ("clean", 0),
    ("no_conclusion", 0),
    ("clean", 0),
    ("showcase", 0),
    ("clean", 0),
    ("showcase", 0),
    ("clean", 0),
    ("no_conclusion", 0),
    # 前两个期间各 1 条，用于演示期间切换后列表仍有内容。
    ("showcase", -2),
    ("clean", -1),
)
RECON_SUPPLIERS_PER_BATCH = 6
RECON_TAX_RATES = {"VAT13": "0.13", "VAT9": "0.09", "IMPORT13": "0.13", "GST9": "0.09", "EXPORT0": "0"}


def build_recon_cases(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    """铺批次输入：最新期间 6 条 + 前两个期间各 1 条。

    这里只造「我方单据 / 供应商对账单」两侧的原始行；批次的 status、metrics、
    差异与证据全部由 ``apps.recon.engine.reconcile`` 跑出来，见
    :func:`build_engine_batches`。禁止在这里手写批次结论。
    """

    latest_period = cfg.periods[-1]
    cutoff = f"{latest_period}-28"
    po_by_no = {row["po_no"]: row for row in data["purchase_order"]}
    po_lines_by_no: dict[str, list[dict]] = defaultdict(list)
    for line in data["purchase_order_line"]:
        po_lines_by_no[line["po_no"]].append(line)
    for lines in po_lines_by_no.values():
        lines.sort(key=lambda row: int(row["line_no"]))
    receipts_by_supplier: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for receipt in data["goods_receipt"]:
        po = po_by_no[receipt["po_no"]]
        if po["order_date"] <= cutoff:
            receipts_by_supplier[receipt["supplier_code"]].append((po, receipt))
    for entries in receipts_by_supplier.values():
        entries.sort(key=lambda pair: (pair[0]["order_date"], pair[0]["po_no"]))

    supplier_rows = {row["supplier_code"]: row for row in data["supplier"]}
    pool = sorted(code for code, entries in receipts_by_supplier.items() if len(entries) >= 2)
    if len(pool) < RECON_SUPPLIERS_PER_BATCH:
        pool = sorted(supplier_rows)

    for batch_idx, (profile, period_offset) in enumerate(RECON_BATCH_PROFILES):
        period = cfg.periods[-1 + period_offset]
        batch_code = (
            f"RB-{period.replace('-', '')}-"
            f"{cfg.code.split('-')[0].upper()}-{batch_idx + 1:02d}"
        )
        chosen = [
            pool[(batch_idx * RECON_SUPPLIERS_PER_BATCH + offset) % len(pool)]
            for offset in range(RECON_SUPPLIERS_PER_BATCH)
        ]
        for s_idx, supplier_code in enumerate(chosen):
            supplier = supplier_rows[supplier_code]
            entries = receipts_by_supplier.get(supplier_code) or []
            if not entries:
                continue
            statement_no = f"ST-{period.replace('-', '')}-{batch_idx + 1:02d}{s_idx + 1:02d}"
            # 每条供应商行都要换一个物料，否则同单号同物料会被引擎判成重复计费。
            available: list[tuple[dict, dict, dict]] = []
            seen_items: set[str] = set()
            for po, receipt in entries:
                for po_line in po_lines_by_no.get(po["po_no"], []):
                    item = po_line["item_code"]
                    if item in seen_items:
                        continue
                    seen_items.add(item)
                    available.append((po, receipt, po_line))
            if not available:
                continue
            cursor = 0

            def take() -> tuple[dict, dict, dict] | None:
                nonlocal cursor
                if cursor >= len(available):
                    return None
                picked = available[cursor]
                cursor += 1
                return picked

            def add_case(
                index: int,
                kind: str,
                picked: tuple[dict, dict, dict],
                *,
                our_qty: Decimal | None,
                sup_qty: Decimal | None,
                our_amount: Decimal,
                sup_amount: Decimal,
                sup_line_no: int,
                our_ref: str,
                supplier_ref: str,
            ) -> None:
                status, needs_manual, match_type = STATEMENT_STATUS[kind]
                data["recon_case"].append(
                    {
                        "scenario": cfg.code,
                        "case_id": f"{batch_code}-{supplier_code}-{index:03d}",
                        "batch_code": batch_code,
                        "period": period,
                        "supplier_code": supplier_code,
                        "supplier_name": supplier["supplier_name"],
                        "line_no": index,
                        "our_ref": our_ref,
                        "supplier_ref": supplier_ref,
                        "item_code": picked[2]["item_code"],
                        "qty_mine": "" if our_qty is None else str(money4(our_qty)),
                        "qty_supplier": "" if sup_qty is None else str(money4(sup_qty)),
                        "our_amount": str(money(our_amount)),
                        "supplier_amount": str(money(sup_amount)),
                        "diff_amount": str(money(sup_amount - our_amount)),
                        "currency": "CNY",
                        "diff_type": kind,
                        "owner": DIFF_OWNER.get(kind, "supplier"),
                        "line_status": status,
                        "needs_manual": "true" if needs_manual else "false",
                        "confidence": {
                            "matched": "0.9990",
                            "duplicate": "0.9940",
                            "missing_doc": "0.9910",
                            "our_missing": "0.9910",
                        }.get(kind, "0.8200"),
                        "note": {
                            "matched": "单据号直配，金额一致",
                            "unit_price": "供应商调价未同步价目表，按合同价复核",
                            "qty": "到货短装，供应商按订单数量开票",
                            "missing_doc": "结算单有行，我方无对应采购/收货单",
                            "duplicate": "同一单据同物料被重复列示",
                            "our_missing": "我方已收货，供应商结算单漏列",
                        }[kind],
                        "_match_type": match_type,
                        "_profile": profile,
                        "_po_no": picked[0]["po_no"],
                        "_receipt_no": picked[1]["receipt_no"],
                    }
                )

            def add_pair(kind: str, picked: tuple[dict, dict, dict], index: int) -> None:
                po, _receipt, po_line = picked
                qty = Decimal(po_line["qty"])
                price = Decimal(po_line["unit_price"])
                tax_rate = RECON_TAX_RATES.get(po_line["tax_code"], "0.13")
                our_ref = f"u8:{po['po_no']}#{po_line['line_no']}"
                supplier_ref = f"statement:{statement_no}#{po_line['line_no']}"
                if kind == "matched":
                    add_case(
                        index, kind, picked,
                        our_qty=qty, sup_qty=qty,
                        our_amount=money(qty * price),
                        sup_amount=money(qty * price),
                        sup_line_no=int(po_line["line_no"]),
                        our_ref=our_ref, supplier_ref=supplier_ref,
                    )
                elif kind == "unit_price":
                    sup_price = money4(price * Decimal("1.035"))
                    add_case(
                        index, kind, picked,
                        our_qty=qty, sup_qty=qty,
                        our_amount=money(qty * price),
                        sup_amount=money(qty * sup_price),
                        sup_line_no=int(po_line["line_no"]),
                        our_ref=our_ref, supplier_ref=supplier_ref,
                    )
                elif kind == "qty":
                    sup_qty = qty + Decimal(rng.randint(2, 18))
                    add_case(
                        index, kind, picked,
                        our_qty=qty, sup_qty=sup_qty,
                        our_amount=money(qty * price),
                        sup_amount=money(sup_qty * price),
                        sup_line_no=int(po_line["line_no"]),
                        our_ref=our_ref, supplier_ref=supplier_ref,
                    )
                elif kind == "duplicate":
                    # 我方一行；供应商同单同物料列两行，第二行由引擎判重复计费。
                    amount = money(qty * price)
                    add_case(
                        index, kind, picked,
                        our_qty=qty, sup_qty=qty,
                        our_amount=amount, sup_amount=amount,
                        sup_line_no=int(po_line["line_no"]),
                        our_ref=our_ref, supplier_ref=supplier_ref,
                    )
                    add_case(
                        index + 1, "duplicate", picked,
                        our_qty=None, sup_qty=qty,
                        our_amount=Decimal("0"), sup_amount=amount,
                        sup_line_no=int(po_line["line_no"]) + 50,
                        our_ref="",
                        supplier_ref=f"statement:{statement_no}#{int(po_line['line_no']) + 50}",
                    )
                elif kind == "missing_doc":
                    amount = money(qty * price)
                    add_case(
                        index, kind, picked,
                        our_qty=None, sup_qty=qty,
                        our_amount=Decimal("0"), sup_amount=amount,
                        sup_line_no=1,
                        our_ref=our_ref,
                        supplier_ref=f"statement:{statement_no}#1",
                    )
                elif kind == "our_missing":
                    amount = money(qty * price)
                    add_case(
                        index, kind, picked,
                        our_qty=qty, sup_qty=None,
                        our_amount=amount, sup_amount=Decimal("0"),
                        sup_line_no=1,
                        our_ref=our_ref,
                        supplier_ref="",
                    )
                _ = tax_rate

            index = 1
            if profile == "clean":
                for _ in range(4):
                    picked = take()
                    if picked is None:
                        break
                    add_pair("matched", picked, index)
                    index += 1
            elif profile == "no_conclusion":
                for _ in range(2):
                    picked = take()
                    if picked is None:
                        break
                    add_pair("matched", picked, index)
                    index += 1
            else:  # showcase：五类必需差异 + 匹配行
                for kind in ("matched", "unit_price", "qty", "duplicate", "missing_doc", "our_missing"):
                    picked = take()
                    if picked is None:
                        break
                    add_pair(kind, picked, index)
                    index += 2 if kind == "duplicate" else 1


def _engine_master_data(
    data: dict[str, list[dict]],
    cases: list[dict],
    *,
    omit_supplier: str | None = None,
) -> dict:
    supplier_rows = {row["supplier_code"]: row for row in data["supplier"]}
    item_rows = {row["item_code"]: row for row in data["item"]}
    supplier_codes = sorted({case["supplier_code"] for case in cases})
    item_codes = sorted({case["item_code"] for case in cases})
    return {
        "suppliers": [
            {
                "code": code,
                "name": supplier_rows[code]["supplier_name"],
                "tax_no": f"91310000{code.replace('-', '')}X",
            }
            for code in supplier_codes
            if code != omit_supplier
        ],
        "items": [
            {
                "code": code,
                "name": item_rows[code]["item_name"],
                "base_unit": item_rows[code]["uom"],
                "spec": item_rows[code]["spec"],
            }
            for code in item_codes
        ],
        "unit_conversions": [],
    }


def _engine_line(
    *,
    system: str,
    ref: str,
    supplier_code: str,
    item_code: str,
    qty: str,
    unit: str,
    unit_price: str,
    amount: str,
    tax_rate: str,
    period: str,
) -> dict:
    doc, _, raw_line = ref.partition("#")
    _, _, doc = doc.partition(":")
    return {
        "system": system,
        "doc_no": doc,
        "line_no": int(raw_line or 1),
        "supplier_code": supplier_code,
        "item_code": item_code,
        "qty": qty,
        "unit": unit,
        "unit_price": unit_price,
        "amount": amount,
        "tax_rate": tax_rate,
        "period": period,
    }


def build_engine_batches(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    """把 ``recon_case`` 转成引擎输入并真实执行 ``reconcile``。

    批次的 status / metrics / result_view / 差异行全部来自引擎返回值；
    这里只决定「喂哪些原始行」，不写任何看起来对的结论。
    """

    item_rows = {row["item_code"]: row for row in data["item"]}
    cases_by_batch: dict[str, list[dict]] = defaultdict(list)
    for case in data["recon_case"]:
        cases_by_batch[case["batch_code"]].append(case)

    engine_batches: dict[str, dict] = {}
    for batch_idx, (batch_code, cases) in enumerate(sorted(cases_by_batch.items())):
        period = cases[0]["period"]
        profile = cases[0].get("_profile", "showcase")
        omit = cases[0]["supplier_code"] if profile == "no_conclusion" else None
        our_lines: list[dict] = []
        supplier_lines: list[dict] = []
        for case in sorted(cases, key=lambda row: row["line_no"]):
            item = item_rows[case["item_code"]]
            kind = case["diff_type"]
            tax_rate = RECON_TAX_RATES.get(item["tax_code"], "0.13")
            our_qty = Decimal(case["qty_mine"]) if case["qty_mine"] else None
            sup_qty = Decimal(case["qty_supplier"]) if case["qty_supplier"] else None
            our_amount = Decimal(case["our_amount"])
            sup_amount = Decimal(case["supplier_amount"])
            if our_qty is not None:
                our_price = money4(our_amount / our_qty) if our_qty else "0"
                our_lines.append(
                    _engine_line(
                        system="u8",
                        ref=case["our_ref"],
                        supplier_code=case["supplier_code"],
                        item_code=case["item_code"],
                        qty=str(our_qty),
                        unit=item["uom"],
                        unit_price=str(our_price),
                        amount=str(our_amount),
                        tax_rate=tax_rate,
                        period=period,
                    )
                )
            if sup_qty is not None:
                sup_price = money4(sup_amount / sup_qty) if sup_qty else "0"
                supplier_lines.append(
                    _engine_line(
                        system="statement",
                        ref=case["supplier_ref"],
                        supplier_code=case["supplier_code"],
                        item_code=case["item_code"],
                        qty=str(sup_qty),
                        unit=item["uom"],
                        unit_price=str(sup_price),
                        amount=str(sup_amount),
                        tax_rate=tax_rate,
                        period=period,
                    )
                )
            _ = kind

        raw = {
            "scope": {
                "org_id": cfg.orgs[0]["org_code"],
                "period": period,
                "system_ids": ["u8", "statement"],
            },
            "suppliers": sorted({case["supplier_code"] for case in cases}),
            "options": {"tolerance": {}},
            "master_data": _engine_master_data(data, cases, omit_supplier=omit),
            "our_lines": our_lines,
            "supplier_lines": supplier_lines,
        }
        result = reconcile(build_request(raw))
        batch_id = uid("batch", cfg.code, batch_code)
        period_end = date.fromisoformat(period + "-01") + timedelta(days=27)
        created = datetime.combine(period_end + timedelta(days=12), datetime.min.time(), TZ).replace(hour=10)
        if created.date() > TODAY - timedelta(days=2):
            created = datetime.combine(TODAY - timedelta(days=2), datetime.min.time(), TZ).replace(hour=10)
        metrics = _metrics_out(result.metrics)
        record = ReconBatchRecord(
            batch_id=batch_id,
            tenant_id=uid("tenant", cfg.code),
            org_id=cfg.orgs[0]["org_code"],
            period=period,
            system_ids=("u8", "statement"),
            suppliers=tuple(sorted({case["supplier_code"] for case in cases})),
            status=result.batch_status.value,
            conclusive=result.conclusive,
            status_reason=result.status_reason,
            metrics=None if metrics is None else metrics.model_dump(mode="json"),
            missing_items=tuple(
                _missing_out(item).model_dump(mode="json") for item in result.missing_items
            ),
            result=result,
            created_by=USERS["initiator"],
            created_at=created,
        )
        fingerprint = hashlib.sha256(
            json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        view = _batch_view(record)
        snapshot = snapshot_from_batch(record, input_fingerprint=fingerprint, view=view)
        if batch_idx % 2 == 0:
            snapshot = replace(
                snapshot,
                unit_price=Decimal("0.8000"),
                currency="CNY",
                unit_price_source=UnitPriceSource.CONTRACT_PRICE,
                price_book_id=uid("price", cfg.code, "CNY"),
            )
        else:
            snapshot = replace(
                snapshot,
                unit_price=Decimal("0.6500"),
                currency="CNY",
                unit_price_source=UnitPriceSource.ENV_FALLBACK,
                price_book_id=None,
            )

        diffs_by_ref = {diff.ref: diff for diff in result.diffs}
        lines_by_ref: dict[str, object] = {}
        for line in result.lines:
            if line.our_ref:
                lines_by_ref[line.our_ref] = line
            if line.supplier_ref:
                lines_by_ref[line.supplier_ref] = line
        for case in cases:
            ref = case["supplier_ref"] or case["our_ref"]
            diff = diffs_by_ref.get(ref)
            if diff is not None:
                case["_engine_diff_type"] = diff.type.name.lower()
                case["_engine_diff_amount"] = str(diff.amount.amount)
                case["_engine_owner"] = diff.owner.value
                case["_engine_needs_manual"] = bool(diff.needs_manual)
            line = lines_by_ref.get(ref)
            if line is not None:
                case["_engine_line_status"] = line.status.value
                case["_engine_line_id"] = None  # 落库时按 ref 关联，见 build_erp_seed

        engine_batches[batch_code] = {
            "snapshot": snapshot,
            "record": record,
            "view": view,
            "period": period,
            "profile": profile,
            "batch_code": batch_code,
        }
        _ = rng
    data["_recon_engine"] = engine_batches


# --------------------------------------------------------------------------- 工单案例


TICKET_PLAN = (
    ("待审批", "W1", "86000.00", "ag2_invoice_fix", "供应商结算单价差 3.5%，待复核后调账"),
    ("待审批", "W2", "268000.00", "ag1_reconcile", "重复计费批次，待业务与财务双人复核"),
    ("待审批", "W2", "735000.00", "ag1_reconcile", "跨期缺单金额较大，需 CFO 终审"),
    ("待确认", "W1", "42300.00", "ag3_inventory_adjust", "库龄超 180 天批次待确认处置方式"),
    ("已驳回", "W1", "58000.00", "ag1_reconcile", "单价差异证据不足，复核驳回"),
    ("执行中", "W2", "312000.00", "ag1_reconcile", "按合同价回写采购发票，执行中"),
    ("校验中", "W2", "196000.00", "ag2_invoice_fix", "红冲重开完成，等待一致性校验"),
    ("已完成", "W1", "96000.00", "ag1_reconcile", "数量差异按实收入账，已完成"),
    ("已完成", "W2", "428000.00", "ag2_invoice_fix", "税率口径修正并重开发票，已完成"),
    ("已完成", "W1", "127000.00", "ag3_inventory_adjust", "报废扣减补录，已完成"),
    ("补偿中", "W2", "355000.00", "ag2_invoice_fix", "下游凭证已生成，正在执行补偿"),
    ("已回滚", "W2", "512000.00", "ag1_reconcile", "调账后总账不平，完整补偿回滚"),
    ("已回滚", "W1", "88000.00", "ag4_masterdata_merge", "主数据合并误并供应商，已回滚"),
    ("需人工处置", "W2", "640000.00", "ag1_reconcile", "补偿在新系统失败，转人工处置"),
    ("已锁定", "W3", "1200000.00", "ag1_reconcile", "不可逆付款指令，锁定待人工解锁"),
    ("已终止", "W1", "39000.00", "ag3_inventory_adjust", "业务取消，工单终止"),
    ("计划中", "W1", "26000.00", "ag1_reconcile", "新识别差异，等待排期处理"),
)

TIMELINES = {
    "计划中": [],
    "待确认": ["待确认"],
    "待审批": ["待审批"],
    "已驳回": ["待审批", "已驳回"],
    "执行中": ["待审批", "执行中"],
    "校验中": ["待审批", "执行中", "校验中"],
    "已完成": ["待审批", "执行中", "校验中", "已完成"],
    "补偿中": ["待审批", "执行中", "校验中", "补偿中"],
    "已回滚": ["待审批", "执行中", "校验中", "补偿中", "已回滚"],
    "需人工处置": ["待审批", "执行中", "补偿中", "需人工处置"],
    "已锁定": ["待审批", "执行中", "已锁定"],
    "已终止": ["已终止"],
}

STEP_ACTIONS = (
    ("write_invoice", "u8", "采购发票", 2),
    ("adjust_receipt", "wms", "收货单", 1),
    ("post_journal", "sap", "会计凭证", 2),
    ("release_hold", "u8", "付款申请", 1),
)

SCENE_TITLES = {
    "ag1_reconcile": "对账差异处置",
    "ag2_invoice_fix": "发票更正",
    "ag3_inventory_adjust": "库存调整",
    "ag4_masterdata_merge": "主数据合并",
}


def build_ticket_cases(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> None:
    cases_by_supplier: dict[str, list[dict]] = defaultdict(list)
    for case in data["recon_case"]:
        cases_by_supplier[case["supplier_code"]].append(case)
    suppliers = list(cases_by_supplier)
    for idx, (state, level, amount, theme, subject) in enumerate(TICKET_PLAN):
        supplier_code = suppliers[idx % len(suppliers)]
        supplier = next(row for row in data["supplier"] if row["supplier_code"] == supplier_code)
        engine_cases = [row for row in cases_by_supplier[supplier_code] if row.get("_engine_diff_type")]
        open_cases = [row for row in cases_by_supplier[supplier_code] if row["needs_manual"] == "true"]
        case = (
            engine_cases[idx % len(engine_cases)]
            if engine_cases
            else (open_cases[idx % len(open_cases)] if open_cases else cases_by_supplier[supplier_code][0])
        )
        # 工单必须晚于对账批次：批次在期末 +12 天生成，工单再顺延 2-8 天。
        period_start = date.fromisoformat(case["period"] + "-01")
        created = period_start + timedelta(days=41 + idx % 7)
        if created >= TODAY - timedelta(days=1):
            created = TODAY - timedelta(days=2)
        ticket_no = f"TK-{created.strftime('%Y%m')}-{idx + 1:04d}"
        amount_value = money(amount)
        initiator_key = "initiator"
        approver_key = "cfo" if amount_value > Decimal("500000") else ("approver" if amount_value > Decimal("100000") else "reviewer")
        data["ticket_case"].append(
            {
                "scenario": cfg.code,
                "ticket_no": ticket_no,
                # 3f4e9c0 只在 roles.DEFAULT_SCENE_CATEGORIES 注册了 ag2_processing_fee；
                # 用未注册场景码会导致审批人点击「通过」被拒，故统一用已注册码，
                # 业务差异放在 subject / business_key / 差异类型里。
                "scene_code": "ag2_processing_fee",
                "ticket_state": state,
                "write_level": level,
                "amount": str(amount_value),
                "touches_fund": "true" if state == "已锁定" or amount_value > Decimal("300000") else "false",
                "org_id": cfg.orgs[0]["org_code"],
                # 演示 scope 固定为「首组织 + 最近期间」，全部工单都能在同一 scope 下看到；
                # 工单实际来源批次仍写在 business_key / story 里。
                "period": cfg.periods[-1],
                "systems": "u8,wms" if theme != "ag2_invoice_fix" else "u8,sap",
                "initiator_name": USER_NAMES[initiator_key],
                "approver_name": USER_NAMES[approver_key],
                "business_key": f"{cfg.code}:{case['batch_code']}:{supplier_code}:{ticket_no}",
                "subject": f"{SCENE_TITLES[theme]} · {supplier['supplier_name']}",
                "story": (
                    f"{subject}；关联结算单 "
                    f"{(case['supplier_ref'] or case['our_ref']).split(':')[1]}，"
                    f"差异 {case.get('_engine_diff_amount', case['diff_amount'])} 元。"
                ),
                "created_date": iso(created),
                "_supplier_code": supplier_code,
                "_case_id": case["case_id"],
                "_batch_code": case["batch_code"],
                "_batch_period": case["period"],
            }
        )


# --------------------------------------------------------------------------- ERP 投影


def _hash_default(value: object) -> str:
    """与 ``apps/platform/contracts/audit.py`` 的冻结口径一致：UTC、6 位微秒、``+00:00``。"""

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="microseconds")
    return str(value)


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_hash_default)


def _event_hash(prev_hash: str, seq: int, event_type: str, from_state: str | None, to_state: str | None, payload: dict, trace_id: str) -> str:
    body = _canonical(
        {"seq": seq, "event_type": event_type, "from_state": from_state, "to_state": to_state, "payload": payload, "trace_id": trace_id}
    )
    return hashlib.sha256(f"{prev_hash}\n{body}".encode()).hexdigest()


def _money_json(value: object) -> dict:
    return {"amount": str(money(value)), "currency": "CNY", "tax_included": True}


def build_erp_seed(cfg: Scenario, data: dict[str, list[dict]], rng: random.Random) -> dict:
    tenant = uid("tenant", cfg.code)
    created_by = USERS["initiator"]
    seed: dict[str, list[dict]] = {
        "recon_batches": [],
        "recon_lines": [],
        "tickets": [],
        "ticket_steps": [],
        "ticket_events": [],
        "audit_images": [],
        "approval_records": [],
        "recon_line_links": [],
        "price_book": [],
    }
    # 价目表：每场景一条 CNY 合同价（计量只从配置取价，这里保留审计口径）
    seed["price_book"].append(
        {
            "id": uid("price", cfg.code, "CNY"),
            "tenant_id": tenant,
            "currency": "CNY",
            "unit_label": "供应商对账单行",
            "unit_price": "0.8000",
            "effective_from": "2026-01-01T00:00:00+08:00",
            "contract_ref": f"HT-{cfg.code.upper()}-2026-001",
            "approved_by_user_id": USERS["cfo"],
            "approved_by_display": USER_NAMES["cfo"],
            "approved_at": "2026-01-05T10:00:00+08:00",
            "supersedes_id": None,
            "created_at": "2026-01-05T10:00:00+08:00",
        }
    )

    cases_by_batch: dict[str, list[dict]] = defaultdict(list)
    for case in data["recon_case"]:
        cases_by_batch[case["batch_code"]].append(case)

    line_ids: dict[str, UUID] = {}
    ref_to_line_id: dict[str, UUID] = {}
    for batch_code, batch in sorted(data["_recon_engine"].items()):
        snapshot = batch["snapshot"]
        record = batch["record"]
        period = batch["period"]
        batch_id = record.batch_id
        for line in snapshot.lines:
            line_id = uid(
                "line",
                cfg.code,
                batch_code,
                line.side,
                line.supplier_ref or line.our_ref or "",
                str(line.line_no),
            )
            if line.supplier_ref:
                ref_to_line_id[line.supplier_ref] = line_id
            if line.our_ref:
                ref_to_line_id[line.our_ref] = line_id
            seed["recon_lines"].append(
                {
                    "id": line_id,
                    "batch_id": batch_id,
                    "tenant_id": tenant,
                    "side": line.side,
                    "line_no": line.line_no,
                    "supplier_code": line.supplier_code,
                    "supplier_ref": line.supplier_ref,
                    "our_ref": line.our_ref,
                    "status": line.status,
                    "match_type": line.match_type,
                    "confidence": line.confidence,
                    "needs_manual": line.needs_manual,
                    "diffs": list(line.diff_facts),
                    "evidence": list(line.evidence),
                    "created_at": record.created_at.isoformat(),
                }
            )
        for case in cases_by_batch.get(batch_code, []):
            for ref in (case["supplier_ref"], case["our_ref"]):
                if ref and ref in ref_to_line_id:
                    line_ids[case["case_id"]] = ref_to_line_id[ref]
                    break
        seed["recon_batches"].append(
            {
                "id": batch_id,
                "tenant_id": tenant,
                "org_id": record.org_id,
                "period": period,
                "status": record.status,
                "conclusive": record.conclusive,
                "status_reason": record.status_reason,
                "suppliers": list(record.suppliers),
                "system_ids": list(record.system_ids),
                "metrics": record.metrics,
                "input_fingerprint": snapshot.input_fingerprint,
                "rerun_of": snapshot.rerun_of,
                "rerun_reason": snapshot.rerun_reason,
                "created_by": record.created_by,
                "created_at": record.created_at.isoformat(),
                "result_view": batch["view"],
                "unit_price": str(snapshot.unit_price),
                "currency": snapshot.currency,
                "unit_price_source": snapshot.unit_price_source.value,
                "price_book_id": snapshot.price_book_id,
            }
        )

    # 工单
    chain_seq = 0
    for idx, case in enumerate(data["ticket_case"]):
        ticket_id = uid("ticket", cfg.code, case["ticket_no"])
        created = datetime.combine(date.fromisoformat(case["created_date"]), datetime.min.time(), TZ).replace(hour=9, minute=20)
        level = int(case["write_level"][1])
        state = case["ticket_state"]
        timeline = TIMELINES[state]
        ticket_events: list[dict] = []
        prev_hash = "0" * 64
        seq = 1
        payload = {"ticket_no": case["ticket_no"], "subject": case["subject"]}
        trace = hashlib.sha256(f"{cfg.code}:{case['ticket_no']}:{seq}".encode()).hexdigest()[:32]
        h = _event_hash(prev_hash, seq, "created", None, "计划中", payload, trace)
        ticket_events.append(
            {
                "id": uid("event", cfg.code, case["ticket_no"], "1"),
                "tenant_id": tenant,
                "ticket_id": ticket_id,
                "seq": seq,
                "event_type": "created",
                "from_state": None,
                "to_state": "计划中",
                "actor_id": USERS["initiator"],
                "payload": payload,
                "trace_id": trace,
                "prev_hash": prev_hash,
                "hash": h,
                "created_at": created.isoformat(),
            }
        )
        prev_hash = h
        current = "计划中"
        event_time = created
        for step_idx, target in enumerate(timeline, start=1):
            seq += 1
            event_time = event_time + timedelta(hours=rng.randint(3, 30))
            trace = hashlib.sha256(f"{cfg.code}:{case['ticket_no']}:{seq}".encode()).hexdigest()[:32]
            h = _event_hash(prev_hash, seq, "transition", current, target, {"reason": case["story"]}, trace)
            ticket_events.append(
                {
                    "id": uid("event", cfg.code, case["ticket_no"], str(seq)),
                    "tenant_id": tenant,
                    "ticket_id": ticket_id,
                    "seq": seq,
                    "event_type": "transition",
                    "from_state": current,
                    "to_state": target,
                    "actor_id": USERS["approver"] if target in ("执行中", "已驳回") else USERS["initiator"],
                    "payload": {"reason": case["story"]},
                    "trace_id": trace,
                    "prev_hash": prev_hash,
                    "hash": h,
                    "created_at": event_time.isoformat(),
                }
            )
            prev_hash = h
            current = target
        seed["ticket_events"].extend(ticket_events)

        seed["tickets"].append(
            {
                "id": ticket_id,
                "tenant_id": tenant,
                "ticket_no": case["ticket_no"],
                "scene_code": case["scene_code"],
                "initiator_id": USERS["initiator"],
                "trigger_kind": "event" if idx % 2 == 0 else "manual",
                "business_key": case["business_key"],
                "state": state,
                "version": len(timeline),
                "diff_amount": case["amount"],
                "systems_count": len(case["systems"].split(",")),
                "touches_fund": case["touches_fund"] == "true",
                "lease_owner": None,
                "lease_expires_at": None,
                "created_at": created.isoformat(),
                "org_id": case["org_id"],
                "period": case["period"],
                "systems": case["systems"].split(","),
                "write_level": level,
                "amount": case["amount"],
                "pending_write": None,
                "updated_at": event_time.isoformat(),
            }
        )

        # 步骤
        step_states: list[str] = []
        if state in ("执行中", "校验中", "已完成", "补偿中", "已回滚", "需人工处置", "已锁定"):
            step_states = ["succeeded", "succeeded"]
            if state == "补偿中":
                step_states[-1] = "compensating"
            elif state == "已回滚":
                step_states = ["compensated", "compensated"] if idx % 2 else ["succeeded", "compensated"]
            elif state == "需人工处置":
                step_states = ["succeeded", "unresolved"]
            elif state == "已锁定":
                step_states = ["succeeded", "failed"]
            elif state == "校验中":
                step_states = ["succeeded", "succeeded"]
        for step_idx, step_state in enumerate(step_states, start=1):
            action, system, object_type, level_step = STEP_ACTIONS[(idx + step_idx) % len(STEP_ACTIONS)]
            step_id = uid("step", cfg.code, case["ticket_no"], str(step_idx))
            executed = created + timedelta(hours=4 * step_idx + 2)
            seed["ticket_steps"].append(
                {
                    "id": step_id,
                    "ticket_id": ticket_id,
                    "step_no": step_idx,
                    "action_key": action,
                    "target_system": system,
                    "write_level": min(level_step, 2),
                    "state": step_state,
                    "idempotency_key": f"{case['ticket_no']}-{step_idx}",
                    "request_hash": hashlib.sha256(f"{case['ticket_no']}:{step_idx}:{action}".encode()).hexdigest(),
                    "request_snapshot": {"object_id": f"{object_type}-{created.strftime('%Y%m')}-{idx + 1:04d}", "amount": case["amount"]},
                    "response_snapshot": {"accepted": step_state != "unresolved", "code": "OK" if step_state != "unresolved" else "E-COMP-01"},
                    "before_ref": f"u8:{object_type}:before:{idx + 1}",
                    "after_ref": f"u8:{object_type}:after:{idx + 1}",
                    "attempt": 1,
                    "latency_ms": rng.randint(120, 1800),
                    "error_code": "E-COMP-01" if step_state == "unresolved" else None,
                    "object_type": object_type,
                    "object_id": f"{object_type}-{created.strftime('%Y%m')}-{idx + 1:04d}",
                    "audit_image_id": None,
                    "executed_at": executed.isoformat(),
                }
            )
            if step_state in ("succeeded", "compensated", "compensating"):
                chain_seq += 1
                image_id = uid("image", cfg.code, case["ticket_no"], str(step_idx))
                # 生产链在 PG 里按 UTC 往返（asyncpg 返回 UTC-aware datetime），
                # 种子必须用同一时区形态参与哈希，否则同一时刻会哈希出两个值。
                image_created = (executed + timedelta(seconds=rng.randint(20, 240))).astimezone(
                    timezone.utc
                )
                base_amount = Decimal(case["amount"])
                if object_type == "收货单":
                    before = {
                        "qty": str(money4(base_amount / Decimal("12.5"))),
                        "amount": str(money(base_amount * Decimal("1.02"))),
                    }
                    after = {
                        "qty": str(money4(base_amount / Decimal("12.5") + Decimal("3"))),
                        "amount": case["amount"],
                    }
                elif object_type == "会计凭证":
                    before = {
                        "account_code": "6602.01",
                        "debit_amount": str(money(base_amount)),
                        "credit_amount": "0.00",
                    }
                    after = {
                        "account_code": "2202.03",
                        "debit_amount": "0.00",
                        "credit_amount": case["amount"],
                    }
                elif object_type == "付款申请":
                    before = {
                        "amount": str(money(base_amount * Decimal("1.035"))),
                        "payee_account": "OLD-6222-****-3108",
                    }
                    after = {
                        "amount": case["amount"],
                        "payee_account": "NEW-6228-****-7742",
                        "payment_date": executed.date().isoformat(),
                    }
                else:  # 采购发票
                    before = {
                        "amount": str(money(base_amount * Decimal("1.035"))),
                        "tax_amount": str(money(base_amount * Decimal("0.09"))),
                        "tax_rate": "0.09",
                    }
                    after = {
                        "amount": case["amount"],
                        "tax_amount": str(money(base_amount * Decimal("0.13"))),
                        "tax_rate": "0.13",
                    }
                prev = seed["audit_images"][-1]["hash"] if seed["audit_images"] else ""
                payload = {
                    "id": image_id,
                    "tenant_id": tenant,
                    "ticket_id": ticket_id,
                    "step_id": step_id,
                    "system": system,
                    "object_type": object_type,
                    "object_id": f"{object_type}-{created.strftime('%Y%m')}-{idx + 1:04d}",
                    "before_ref": f"u8:{object_type}:before:{idx + 1}",
                    "after_ref": f"u8:{object_type}:after:{idx + 1}",
                    # 与生产 append_image 一致：diff 由生产函数生成（含 delta 键）
                    "field_diff": [
                        entry.model_dump(mode="python")
                        for entry in compute_field_diff(before, after)
                    ],
                    "diff_missing": False,
                    "prev_hash": prev,
                    "hash": "0" * 64,
                    "created_at": image_created,
                }
                image = AuditImage.model_validate(payload)
                digest = compute_image_hash(prev, image.model_dump(mode="python"))
                image = image.model_copy(update={"hash": digest})
                row = image.model_dump(mode="json")
                row["created_at"] = image.created_at.isoformat()
                seed["audit_images"].append(row)
                for step in seed["ticket_steps"]:
                    if step["id"] == step_id:
                        step["audit_image_id"] = image_id

        # 审批记录
        if "待审批" in timeline:
            amount_value = Decimal(case["amount"])
            roles: list[str] = ["reviewer"] if level == 1 else ["approver"]
            if amount_value > Decimal("100000") or case["touches_fund"] == "true":
                if "approver" not in roles:
                    roles.append("approver")
                if len(roles) < 2:
                    roles.insert(0, "reviewer")
            if amount_value > Decimal("500000"):
                roles.append("cfo")
            decided = created + timedelta(hours=6)
            if state == "待审批":
                roles = roles[:-1]
            for role in roles:
                decided = decided + timedelta(hours=3)
                seed["approval_records"].append(
                    {
                        "id": uid("approval", cfg.code, case["ticket_no"], role),
                        "tenant_id": tenant,
                        "ticket_id": ticket_id,
                        "step_id": None,
                        "role": role,
                        "approver_id": USERS[role],
                        "initiator_id": USERS["initiator"],
                        "approver_roles": [role],
                        "decision": "rejected" if state == "已驳回" else "approved",
                        "comment": "证据链完整，同意处理" if state != "已驳回" else "差异证据不足，退回补充合同价附件",
                        "exception_reason": None,
                        "decided_at": decided.isoformat(),
                    }
                )

        # 对账行与工单的关联
        if case["_case_id"] in line_ids:
            seed["recon_line_links"].append(
                {
                    "id": uid("link", cfg.code, case["ticket_no"]),
                    "tenant_id": tenant,
                    "line_id": line_ids[case["_case_id"]],
                    "ticket_id": ticket_id,
                    "audit_image_id": None,
                    "link_type": "created",
                    "created_at": (created + timedelta(hours=1)).isoformat(),
                }
            )
    return seed


# --------------------------------------------------------------------------- 校验


BANNED = ("测试", "test", "TEST", "abc", "ABC", "样例")


def validate(cfg: Scenario, data: dict[str, list[dict]], erp: dict) -> dict:
    problems: list[str] = []
    latest = max(
        [date.fromisoformat(row["order_date"]) for row in data["sales_order"]]
        + [date.fromisoformat(row["order_date"]) for row in data["purchase_order"]]
    )
    if latest > TODAY:
        problems.append(f"存在未来订单日期 {latest}")
    if latest < WINDOW_START:
        problems.append("订单日期早于 12 个月窗口")

    # 头行勾稽
    so_lines: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    so_tax: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in data["sales_order_line"]:
        so_lines[row["so_no"]] += Decimal(row["amount"])
        so_tax[row["so_no"]] += Decimal(row["tax_amount"])
    for row in data["sales_order"]:
        if money(so_lines[row["so_no"]]) != Decimal(row["subtotal"]):
            problems.append(f"{row['so_no']} 行合计与订单头不一致")
        if money(so_tax[row["so_no"]]) != Decimal(row["tax_amount"]):
            problems.append(f"{row['so_no']} 行税额与订单头不一致")
        if Decimal(row["subtotal"]) + Decimal(row["tax_amount"]) != Decimal(row["total_amount"]):
            problems.append(f"{row['so_no']} 价税合计不平")

    po_lines: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in data["purchase_order_line"]:
        po_lines[row["po_no"]] += Decimal(row["amount"])
    for row in data["purchase_order"]:
        if money(po_lines[row["po_no"]]) != Decimal(row["subtotal"]):
            problems.append(f"{row['po_no']} 行合计与订单头不一致")

    # 发货 <= 订单、发票 <= 发货
    ordered: dict[tuple[str, int], Decimal] = {}
    for row in data["sales_order_line"]:
        ordered[(row["so_no"], row["line_no"])] = Decimal(row["qty"])
    shipped: dict[tuple[str, int], Decimal] = defaultdict(lambda: Decimal("0"))
    for row in data["sales_delivery_line"]:
        shipped[(row["so_no"], row["so_line_no"])] += Decimal(row["qty"])
    for key, qty in shipped.items():
        if qty > ordered.get(key, Decimal("0")) + Decimal("0.0001"):
            problems.append(f"发货超订单 {key}")
    invoiced: dict[tuple[str, int], Decimal] = defaultdict(lambda: Decimal("0"))
    for row in data["sales_invoice_line"]:
        invoiced[(row["delivery_no"], row["delivery_line_no"])] += Decimal(row["qty"])
    delivered: dict[tuple[str, int], Decimal] = {}
    for row in data["sales_delivery_line"]:
        delivered[(row["delivery_no"], row["line_no"])] = Decimal(row["qty"])
    for key, qty in invoiced.items():
        if qty > delivered.get(key, Decimal("0")) + Decimal("0.0001"):
            problems.append(f"开票超发货 {key}")

    # 采购：收货 <= 订单，发票 <= 收货
    po_ordered: dict[tuple[str, int], Decimal] = {}
    for row in data["purchase_order_line"]:
        po_ordered[(row["po_no"], row["line_no"])] = Decimal(row["qty"])
    received: dict[tuple[str, int], Decimal] = defaultdict(lambda: Decimal("0"))
    for row in data["goods_receipt_line"]:
        received[(row["po_no"], row["po_line_no"])] += Decimal(row["qty_received"])
    for key, qty in received.items():
        if qty > po_ordered.get(key, Decimal("0")) + Decimal("0.0001"):
            problems.append(f"收货超采购订单 {key}")

    # 业务顺序：下单 < 发货/收货 < 开票 < 对账 < 工单
    so_date = {row["so_no"]: row["order_date"] for row in data["sales_order"]}
    delivery_date = {row["delivery_no"]: row["delivery_date"] for row in data["sales_delivery"]}
    for row in data["sales_delivery"]:
        if so_date[row["so_no"]] >= row["delivery_date"]:
            problems.append(f"{row['delivery_no']} 发货不晚于订单")
    for row in data["sales_invoice"]:
        if delivery_date[row["delivery_no"]] >= row["invoice_date"]:
            problems.append(f"{row['invoice_no']} 开票不晚于发货")
    po_date = {row["po_no"]: row["order_date"] for row in data["purchase_order"]}
    receipt_date = {row["receipt_no"]: row["receipt_date"] for row in data["goods_receipt"]}
    for row in data["goods_receipt"]:
        if po_date[row["po_no"]] >= row["receipt_date"]:
            problems.append(f"{row['receipt_no']} 收货不晚于采购订单")
    for row in data["purchase_invoice"]:
        if receipt_date[row["receipt_no"]] >= row["invoice_date"]:
            problems.append(f"{row['invoice_no']} 开票不晚于收货")
    for row in data["ticket_case"]:
        batch_finish = date.fromisoformat(row["_batch_period"] + "-01") + timedelta(days=39)
        if row["created_date"] <= iso(batch_finish):
            problems.append(f"{row['ticket_no']} 工单不晚于对账批次")

    # 借贷平衡
    debit: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    credit: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in data["gl_entry"]:
        debit[row["voucher_no"]] += Decimal(row["debit"])
        credit[row["voucher_no"]] += Decimal(row["credit"])
    for voucher, value in debit.items():
        if value != credit[voucher]:
            problems.append(f"凭证 {voucher} 借贷不平")
        if value == 0:
            problems.append(f"凭证 {voucher} 金额为零")

    # 差异类型 / 批次状态 / 计量来源 / 工单状态覆盖（以引擎实际产物为准）
    types = {
        diff["type"]
        for batch in erp["recon_batches"]
        for diff in (batch.get("result_view") or {}).get("diffs", [])
    }
    required = {"unit_price", "qty", "missing_doc", "duplicate"}
    if not required <= types:
        problems.append(f"差异类型缺失 {required - types}")
    line_status = {row["status"] for row in erp["recon_lines"]}
    if "我方未匹配" not in line_status or "供应商未匹配" not in line_status:
        problems.append(f"缺单方向不全：{sorted(line_status)}")
    batch_states = {row["status"] for row in erp["recon_batches"]}
    if len(erp["recon_batches"]) < 6:
        problems.append(f"对账批次不足 6 条：{len(erp['recon_batches'])}")
    in_default_scope = [
        row
        for row in erp["recon_batches"]
        if row["period"] == cfg.periods[-1] and row["org_id"] == cfg.orgs[0]["org_code"]
    ]
    if len(in_default_scope) <= 10:
        problems.append(f"默认 scope 批次不足以翻页（前端每页 10）：{len(in_default_scope)}")
    for state in ("已对账", "待人工复核", "无法对账"):
        if state not in batch_states:
            problems.append(f"批次状态缺失 {state}：{sorted(batch_states)}")
    price_sources = {row["unit_price_source"] for row in erp["recon_batches"]}
    if price_sources != {"contract_price", "env_fallback"}:
        problems.append(f"单价来源未覆盖封顶/非封顶配置两例：{sorted(price_sources)}")

    states = {row["ticket_state"] for row in data["ticket_case"]}
    if len(states) < 12:
        problems.append(f"工单状态覆盖不足：{sorted(states)}")
    if not any(row["ticket_state"] == "已回滚" for row in data["ticket_case"]):
        problems.append("缺少完整补偿回滚链路")

    # 审计影像：至少两张工单各有多张影像，且前后像字段集合不同
    images_by_ticket: dict[str, list[dict]] = defaultdict(list)
    for image in erp["audit_images"]:
        images_by_ticket[str(image["ticket_id"])].append(image)
    varied = [
        ticket_id
        for ticket_id, images in images_by_ticket.items()
        if len(images) >= 2
        and len({tuple(sorted(entry["field"] for entry in image["field_diff"])) for image in images}) >= 2
    ]
    if len(varied) < 2:
        problems.append(f"多影像且字段差异不同的工单不足 2 张：{len(varied)}")

    # 未来时间戳
    for table in ("sales_order", "sales_delivery", "sales_invoice", "purchase_order", "goods_receipt", "purchase_invoice", "production_order", "ticket_case", "recon_case"):
        for row in data.get(table, []):
            for key, value in row.items():
                if key.endswith("_date") and value and str(value) > iso(TODAY):
                    problems.append(f"{table}.{key} 出现未来日期 {value}")

    for table, rows in data.items():
        if table.startswith("_"):
            continue
        for row in rows:
            for value in row.values():
                text = str(value)
                if any(bad in text for bad in BANNED):
                    problems.append(f"{table} 出现禁用词：{text[:40]}")
                    break

    dedupe = {row["item_code"] for row in data["item"]}
    if len(dedupe) != len(data["item"]):
        problems.append("物料编码重复")
    return {
        "ok": not problems,
        "problems": problems,
        "latest_business_date": iso(latest),
        "diff_types": sorted(types),
        "ticket_states": sorted(states),
        "compensation_chains": sum(1 for row in data["ticket_case"] if row["ticket_state"] == "已回滚"),
    }


# --------------------------------------------------------------------------- 落盘


TABLES = (
    "scenario", "organization", "warehouse", "currency", "tax_rate",
    "customer", "supplier", "item", "bom_line",
    "sales_order", "sales_order_line", "sales_delivery", "sales_delivery_line",
    "sales_invoice", "sales_invoice_line",
    "purchase_order", "purchase_order_line", "goods_receipt", "goods_receipt_line",
    "purchase_invoice", "purchase_invoice_line", "inventory_lot",
    "production_order", "production_material_issue", "gl_entry",
    "recon_case", "ticket_case",
    "promotion", "rebate_accrual", "fx_rate", "customs_declaration", "intercompany_settlement",
)


def write_scenario(cfg: Scenario) -> dict:
    rng = random.Random(stable_seed(cfg.code))
    data: dict[str, list[dict]] = defaultdict(list)
    master = build_master(cfg, rng)
    for key, rows in master.items():
        data[key].extend(rows)
    build_bom(cfg, data, rng)
    build_sales(cfg, data, rng)
    build_purchase(cfg, data, rng)
    build_production(cfg, data, rng)
    build_gl(cfg, data, rng)
    if cfg.scenario_extra == "fmcg":
        build_fmcg_extra(cfg, data, rng)
    if cfg.scenario_extra == "crossborder":
        build_fx_rate(cfg, data, rng)
        build_xb_extra(cfg, data, rng)
    build_recon_cases(cfg, data, rng)
    build_engine_batches(cfg, data, rng)
    build_ticket_cases(cfg, data, rng)
    erp = build_erp_seed(cfg, data, rng)

    checks = validate(cfg, data, erp)
    out_dir = SCEN_DIR / cfg.code
    out_dir.mkdir(parents=True, exist_ok=True)
    file_hashes: dict[str, str] = {}
    row_counts: dict[str, int] = {}
    for table in TABLES:
        rows = data.get(table, [])
        row_counts[table] = len(rows)
        path = out_dir / f"{table}.csv"
        if not rows:
            if path.exists():
                path.unlink()
            continue
        fields = [key for key in rows[0] if not key.startswith("_")]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: ("" if row.get(key) is None else row[key]) for key in fields})
        file_hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()

    erp_path = out_dir / "erp_seed.json"
    erp_path.write_text(
        json.dumps({"scenario": cfg.code, "tenant_id": str(uid("tenant", cfg.code)), **erp}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
        newline="\n",
    )
    file_hashes[erp_path.name] = hashlib.sha256(erp_path.read_bytes()).hexdigest()

    erp_counts = {key: len(value) for key, value in erp.items()}
    manifest = {
        "scenario": cfg.code,
        "display_name": cfg.display_name,
        "industry": cfg.industry,
        "story": cfg.story,
        "base_currency": cfg.base_currency,
        "timezone": "Asia/Shanghai",
        "generated_at": ts(datetime(2026, 11, 11, 9, 0, tzinfo=TZ)),
        "seed": stable_seed(cfg.code),
        "window": {"start": iso(WINDOW_START), "end": iso(TODAY)},
        "tenant_id": str(uid("tenant", cfg.code)),
        "demo_scope": {"org_id": cfg.orgs[0]["org_code"], "period": cfg.periods[-1], "system_ids": ["u8", "statement"]},
        "demo_users": {key: {"id": str(value), "display": USER_NAMES[key]} for key, value in USERS.items()},
        "row_counts": row_counts,
        "erp_counts": erp_counts,
        "checks": checks,
        "files": file_hashes,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 smart-ERP demo 数据集")
    parser.add_argument("--scenario", action="append", choices=sorted(SCENARIOS), help="只生成指定场景，可重复")
    args = parser.parse_args()
    codes = args.scenario or list(SCENARIOS)
    SCEN_DIR.mkdir(parents=True, exist_ok=True)
    manifests = []
    failed = False
    for code in codes:
        manifest = write_scenario(SCENARIOS[code])
        manifests.append({key: manifest[key] for key in ("scenario", "display_name", "row_counts", "erp_counts", "checks")})
        counts = manifest["row_counts"]
        print(
            f"[{code}] 客户 {counts['customer']} 供应商 {counts['supplier']} 物料 {counts['item']} "
            f"销售行 {counts['sales_order_line']} 采购行 {counts['purchase_order_line']} "
            f"对账案例 {counts['recon_case']} 工单 {counts['ticket_case']}"
        )
        if manifest["checks"]["problems"]:
            failed = True
            for problem in manifest["checks"]["problems"]:
                print(f"  ! {problem}")
        else:
            print(f"  勾稽校验通过；差异类型 {', '.join(manifest['checks']['diff_types'])}")
    (SCEN_DIR / "index.json").write_text(
        json.dumps({"generated_at": ts(datetime(2026, 11, 11, 9, 0, tzinfo=TZ)), "scenarios": manifests}, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
