"""0008：批次取价快照与可区分原因的 503。

三条不能假绿的约束：

1. ``E-PRICE-MISSING``（压根没价）与 ``E-PRICE-CURRENCY``（有价但币种不符）是两个码，
   不能复用 ``E-DATA-MISSING``；错误体带排障上下文，但不回显其他币种的单价；
2. 取价失败时批次四字段全 NULL（允许批次落库供审计，但不许写「半套快照」）；
3. ``REQUIRE=false`` 时同一场景必须 200 + ``unit_price_source=env_fallback``，
   证明 503 不是「这个接口总是 503」。
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import PERIOD, PREFIX, STATEMENT, SYSTEM

from apps.api.price_book import PriceLookup, PriceQuote
from apps.api.recon_snapshot import UnitPriceSource

UNIT_PRICE = "0.30"
OTHER_CURRENCY_PRICE = "9.9900"


class FakePriceBook:
    """价目端口替身：可配置「有价 / 只有别种币种的价」，并记录被问到的币种。"""

    def __init__(
        self,
        *,
        quote: PriceQuote | None = None,
        has_other_currency_price: bool = False,
    ) -> None:
        self.quote = quote
        self.has_other_currency_price = has_other_currency_price
        self.calls: list[tuple[object, str, object]] = []

    async def effective_price(self, *, tenant_id, currency, at) -> PriceLookup:
        self.calls.append((tenant_id, currency, at))
        return PriceLookup(
            quote=self.quote,
            has_other_currency_price=self.has_other_currency_price,
        )


def _line(*, system: str, doc_no: str, amount: str = "50.00") -> dict[str, object]:
    return {
        "system": system,
        "doc_no": doc_no,
        "line_no": 1,
        "supplier_code": "SUP-A",
        "item_code": "IT-A",
        "qty": "10",
        "unit": "PCS",
        "unit_price": "5",
        "amount": amount,
        "tax_rate": "0.13",
        "period": PERIOD,
    }


def _body(env, *, dataset: bool = True) -> dict[str, object]:
    body: dict[str, object] = {
        "scope": env.scope(system_ids=[SYSTEM, STATEMENT]),
        "suppliers": ["SUP-A"],
    }
    if dataset:
        body["dataset"] = {
            "our_lines": [_line(system=SYSTEM, doc_no="PO-1")],
            "supplier_lines": [_line(system=STATEMENT, doc_no="PO-1")],
            "master_data": {
                "suppliers": [{"code": "SUP-A", "name": "供应商A"}],
                "items": [{"code": "IT-A", "name": "物料A", "base_unit": "PCS"}],
            },
            "tolerance": {},
        }
    return body


def _persisted_batch(env, batch_id: str | None = None) -> dict[str, object]:
    payload = json.loads(Path(env.recon_store_path).read_text(encoding="utf-8"))
    if batch_id is None:
        assert len(payload["batches"]) == 1, payload["batches"]
        return next(iter(payload["batches"].values()))
    return payload["batches"][batch_id]


def _assert_all_price_fields_null(row: dict[str, object]) -> None:
    assert row["unit_price"] is None
    assert row["currency"] is None
    assert row["unit_price_source"] is None
    assert row["price_book_id"] is None


def test_require_contract_price_without_price_returns_price_missing(env, monkeypatch):
    monkeypatch.setenv("ERP_METERING_REQUIRE_CONTRACT_PRICE", "true")
    monkeypatch.setenv("ERP_METERING_CURRENCY", "CNY")
    monkeypatch.setenv("ERP_METERING_UNIT_PRICE", UNIT_PRICE)  # 有 env 价也不许回退
    fake = FakePriceBook()
    env.container.price_book = fake

    response = env.client.post(
        f"{PREFIX}/recon/runs", json=_body(env), headers=env.headers("initiator", "k-price-missing")
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "E-PRICE-MISSING"
    assert body["code"] != "E-DATA-MISSING"
    # 排障上下文：能定位到「谁、什么币种、哪个时刻」，但不含任何单价。
    assert body["context"]["currency"] == "CNY"
    assert body["context"]["tenant_id"]
    assert body["context"]["at"]
    assert "unit_price" not in body
    # 失败批次已落库，但四字段必须整组 NULL（不允许写一半）。
    row = _persisted_batch(env)
    _assert_all_price_fields_null(row)
    # 失败批次不进入内存「成功批次」列表；它只是审计记录。
    assert env.container.recon_batches.get(uuid.UUID(row["batch_id"])) is None
    # 取价确实发生在建批次时，而不是账单读取时才取。
    assert [call[1] for call in fake.calls] == ["CNY"]


def test_currency_mismatch_is_a_distinct_code_and_does_not_leak_prices(env, monkeypatch):
    monkeypatch.setenv("ERP_METERING_REQUIRE_CONTRACT_PRICE", "true")
    monkeypatch.setenv("ERP_METERING_CURRENCY", "CNY")
    fake = FakePriceBook(has_other_currency_price=True)
    env.container.price_book = fake

    response = env.client.post(
        f"{PREFIX}/recon/runs",
        json=_body(env),
        headers=env.headers("initiator", "k-price-currency"),
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "E-PRICE-CURRENCY"
    assert body["code"] != "E-PRICE-MISSING"
    # 反向：不能把价格表里其他币种的单价带进响应（那是别家的合同价）。
    assert OTHER_CURRENCY_PRICE not in response.text
    assert "9900" not in response.text
    _assert_all_price_fields_null(_persisted_batch(env))


def test_require_contract_price_false_falls_back_to_env(env, monkeypatch):
    monkeypatch.setenv("ERP_METERING_REQUIRE_CONTRACT_PRICE", "false")
    monkeypatch.setenv("ERP_METERING_CURRENCY", "CNY")
    monkeypatch.setenv("ERP_METERING_UNIT_PRICE", UNIT_PRICE)
    env.container.price_book = FakePriceBook()

    response = env.client.post(
        f"{PREFIX}/recon/runs", json=_body(env), headers=env.headers("initiator", "k-env-fallback")
    )

    assert response.status_code == 201, response.text
    row = _persisted_batch(env, response.json()["batch_id"])
    assert Decimal(row["unit_price"]) == Decimal("0.3000")
    assert row["currency"] == "CNY"
    assert row["unit_price_source"] == "env_fallback"
    assert row["price_book_id"] is None

    statement = env.client.get(
        f"{PREFIX}/metering/batches/{response.json()['batch_id']}/statement",
        params=env.scope_params(system_ids=[SYSTEM, STATEMENT]),
        headers=env.headers(),
    )
    assert statement.status_code == 200, statement.text
    assert statement.json()["unit_price_source"] == "env_fallback"


def test_contract_price_quote_is_snapshotted_with_its_source(env, monkeypatch):
    monkeypatch.setenv("ERP_METERING_REQUIRE_CONTRACT_PRICE", "true")
    monkeypatch.setenv("ERP_METERING_CURRENCY", "CNY")
    quote_id = "44444444-4444-4444-4444-444444444444"
    env.container.price_book = FakePriceBook(
        quote=PriceQuote(
            price_book_id=uuid.UUID(quote_id),
            unit_price=Decimal("0.1234"),
            currency="CNY",
        )
    )

    response = env.client.post(
        f"{PREFIX}/recon/runs", json=_body(env), headers=env.headers("initiator", "k-contract")
    )

    assert response.status_code == 201, response.text
    row = _persisted_batch(env, response.json()["batch_id"])
    assert row["unit_price"] == "0.1234"
    assert row["currency"] == "CNY"
    assert row["unit_price_source"] == "contract_price"
    assert row["price_book_id"] == quote_id


def test_missing_dataset_still_returns_data_missing(env, monkeypatch):
    """反向断言：两个价目码不能把「缺 dataset」也吞掉。"""

    monkeypatch.setenv("ERP_METERING_REQUIRE_CONTRACT_PRICE", "true")
    env.container.price_book = FakePriceBook()

    response = env.client.post(
        f"{PREFIX}/recon/runs",
        json=_body(env, dataset=False),
        headers=env.headers("initiator", "k-no-dataset"),
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "E-DATA-MISSING"


def test_no_usable_price_uses_one_code_regardless_of_require_switch(env, monkeypatch):
    """口径 A：错误码表示发生了什么，开关取值只进 context。

    - require=true + 无合同价 → 建批次 503 `E-PRICE-MISSING`，`source=contract`；
    - require=false + 无合同价 + env 未配置 → 建批次 201，但账单读取仍 503
      **同一个码**，`source=env`；不得静默出 0 元账单。

    任一支改回 `E-DATA-MISSING`、或按开关给出两个不同码，这条用例都会红。
    """

    monkeypatch.setenv("ERP_METERING_CURRENCY", "CNY")
    monkeypatch.delenv("ERP_METERING_UNIT_PRICE", raising=False)
    monkeypatch.setenv("ERP_METERING_REQUIRE_CONTRACT_PRICE", "true")
    env.container.price_book = FakePriceBook()

    strict = env.client.post(
        f"{PREFIX}/recon/runs", json=_body(env), headers=env.headers("initiator", "k-a-strict")
    )
    assert strict.status_code == 503, strict.text
    strict_body = strict.json()
    assert strict_body["code"] == "E-PRICE-MISSING"
    assert strict_body["context"]["source"] == "contract"
    assert strict_body["context"]["require_contract_price"] == "true"

    # 同一根因，仅开关不同：批次可以先建，但出账必须仍用同一个码。
    monkeypatch.setenv("ERP_METERING_REQUIRE_CONTRACT_PRICE", "false")
    loose_body = _body(env)
    loose_body["dataset"]["our_lines"][0]["doc_no"] = "PO-2"
    loose_body["dataset"]["supplier_lines"][0]["doc_no"] = "PO-2"
    run = env.client.post(
        f"{PREFIX}/recon/runs", json=loose_body, headers=env.headers("initiator", "k-a-loose")
    )
    assert run.status_code == 201, run.text

    loose = env.client.get(
        f"{PREFIX}/metering/batches/{run.json()['batch_id']}/statement",
        params=env.scope_params(system_ids=[SYSTEM, STATEMENT]),
        headers=env.headers(),
    )
    assert loose.status_code == 503, loose.text
    loose_body_json = loose.json()
    assert loose_body_json["code"] == "E-PRICE-MISSING"
    assert loose_body_json["code"] != "E-DATA-MISSING"
    assert loose_body_json["context"]["source"] == "env"
    assert loose_body_json["context"]["require_contract_price"] == "false"


def test_unit_price_source_is_a_closed_enum() -> None:
    assert UnitPriceSource.parse("contract_price") is UnitPriceSource.CONTRACT_PRICE
    assert UnitPriceSource.parse("env_fallback") is UnitPriceSource.ENV_FALLBACK
    # 旧迁移里出现过的自由值不再被接受，未知值必须报错。
    for unknown in ("snapshot", "lookback", "missing", "ERP_METERING_UNIT_PRICE", ""):
        with pytest.raises(ValueError):
            UnitPriceSource.parse(unknown)
