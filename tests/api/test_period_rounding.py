"""账期取整口径：批次级 4 位不取整，账期汇总层一次 ``ROUND_HALF_UP``。

算例即断言：20 × ``0.3333`` = ``6.6666`` → ``6.67``；
若逐批次先取整再相加（``0.33 × 20``）= ``6.60``，差 1 分。
客户按批次核、按总额付，两边对不上就是争议——这条用例是「为什么不在批次层取整」
的唯一实证，改口径必须先改它。
"""

from decimal import ROUND_HALF_UP, Decimal

from apps.api.metering import settle_period_amount

BATCH_AMOUNT = Decimal("0.3333")
BATCHES = [BATCH_AMOUNT] * 20


def test_period_settlement_rounds_once_over_the_sum() -> None:
    assert settle_period_amount(BATCHES) == Decimal("6.67")


def test_per_batch_rounding_would_short_by_seven_cents() -> None:
    per_batch = sum(
        (
            amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for amount in BATCHES
        ),
        Decimal(0),
    )
    period = settle_period_amount(BATCHES)

    assert per_batch == Decimal("6.60")
    assert period != per_batch
    # 20 × 0.3333 = 6.6666：逐批次舍掉 0.0033/批，累计差 0.0666 元（7 分），
    # 不是 1 分；「差 1 分」只在小额场景成立，见下一条用例。
    assert period - per_batch == Decimal("0.07")


def test_small_batches_differ_by_exactly_one_cent() -> None:
    # 2 × 0.0025 = 0.005 → 汇总层 HALF_UP 得 0.01；逐批次 0.00 + 0.00 = 0.00
    batches = [Decimal("0.0025"), Decimal("0.0025")]
    per_batch = sum(
        (
            amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for amount in batches
        ),
        Decimal(0),
    )

    assert settle_period_amount(batches) == Decimal("0.01")
    assert per_batch == Decimal("0.00")
    assert settle_period_amount(batches) - per_batch == Decimal("0.01")


def test_settlement_uses_half_up_not_bankers_rounding() -> None:
    assert settle_period_amount([Decimal("6.665")]) == Decimal("6.67")
    # 反例：Python 默认 HALF_EVEN 会得到 6.66（少 1 分）
    assert Decimal("6.665").quantize(Decimal("0.01")) == Decimal("6.66")
