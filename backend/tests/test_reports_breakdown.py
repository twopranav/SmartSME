"""What a downloaded report says at a glance: profit or loss, what was sold,
and what is on the shelf.

The stock picture has to follow the products themselves, so a product added
after the last purchase, or stock typed in by hand, is in it straight away.
"""

from __future__ import annotations

import uuid

import pytest

from app.reports import (
    SLICE_COLOURS,
    Breakdown,
    BreakdownRow,
    _profit_figure,
    _with_shares,
    breakdown_slices,
)
from tests.conftest import _new_client


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------
def _breakdown(amounts: list[float]) -> Breakdown:
    rows = [BreakdownRow(f"Item {i}", "kg", 1, a, 0.0) for i, a in enumerate(amounts, start=1)]
    return _with_shares("itemsSold", "Items sold", "", "Sold", "Sales value", rows)


def test_shares_add_up_and_the_largest_comes_first():
    b = _breakdown([100, 300, 600])
    assert [r.amount for r in b.rows] == [600, 300, 100]
    assert [r.share for r in b.rows] == [60.0, 30.0, 10.0]
    assert b.total == 1000


def test_a_few_items_get_a_slice_each():
    slices = breakdown_slices(_breakdown([50, 30, 20]))
    assert [s.label for s in slices] == ["Item 1", "Item 2", "Item 3"]
    assert [s.colour for s in slices] == list(SLICE_COLOURS[:3])


def test_a_long_tail_is_folded_into_one_slice_that_keeps_the_sum():
    b = _breakdown([400, 200, 150, 100, 60, 40, 30, 15, 5])
    slices = breakdown_slices(b)
    assert len(slices) == len(SLICE_COLOURS) + 1
    other = slices[-1]
    assert other.label.startswith("Other (4 more items")
    assert other.amount == 90
    # Nothing is lost by the folding: the slices are still the whole.
    assert sum(s.amount for s in slices) == pytest.approx(b.total)
    assert sum(s.share for s in slices) == pytest.approx(100, abs=0.2)


def test_a_short_tail_is_named_rather_than_hidden():
    slices = breakdown_slices(_breakdown([400, 200, 150, 100, 60, 40, 30]))
    assert slices[-1].label == "Other: Item 6, Item 7"
    # A single leftover is simply itself.
    assert breakdown_slices(_breakdown([400, 200, 150, 100, 60, 40]))[-1].label == "Item 6"


def test_nothing_worth_anything_draws_nothing():
    assert breakdown_slices(_breakdown([0, 0])) == []
    assert breakdown_slices(_breakdown([])) == []


@pytest.mark.parametrize(
    ("net", "gross_only", "label", "amount", "tone"),
    [
        (1500.0, False, "Net profit", 1500.0, "good"),
        (-420.5, False, "Net loss", 420.5, "bad"),
        (800.0, True, "Gross profit", 800.0, "good"),
        (-1.0, True, "Gross loss", 1.0, "bad"),
        (0.0, False, "Break-even", 0.0, "even"),
    ],
)
def test_profit_is_named_and_a_loss_is_a_positive_amount_called_a_loss(
    net, gross_only, label, amount, tone
):
    figure = _profit_figure(net, gross_only=gross_only)
    assert (figure.label, figure.amount, figure.tone) == (label, amount, tone)


# ---------------------------------------------------------------------------
# Through the API, on a business of its own
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def shop(anon):
    """A fresh business: tax 18% (the default), two products, three sales."""
    c = _new_client()
    c.__enter__()
    r = c.post(
        "/api/auth/sign-up",
        json={
            "businessName": f"Report Stores {uuid.uuid4().hex[:6]}",
            "name": "Owner",
            "email": f"report-{uuid.uuid4().hex[:10]}@smoketest.dev",
            "password": "report-1234",
        },
    )
    assert r.status_code == 201, r.text

    def product(name: str, buy: float, sell: float, stock: int) -> str:
        made = c.post(
            "/api/products",
            json={
                "name": name,
                "unit": "kg",
                "purchasePrice": buy,
                "sellingPrice": sell,
                "stock": stock,
            },
        )
        assert made.status_code == 201, made.text
        return made.json()["id"]

    rice = product("Rice", 40, 50, 100)
    tea = product("Tea", 300, 400, 20)

    def sell(lines: list[tuple[str, str, int, float]]) -> str:
        made = c.post(
            "/api/sales",
            json={
                "items": [
                    {"productId": pid, "description": name, "quantity": q, "unitPrice": price}
                    for pid, name, q, price in lines
                ]
            },
        )
        assert made.status_code == 201, made.text
        return made.json()["id"]

    sell([(rice, "Rice", 10, 50), (tea, "Tea", 2, 400)])  # 500 + 800
    sell([(rice, "Rice", 20, 50)])  # 1000
    cancelled = sell([(tea, "Tea", 5, 400)])  # 2000, then cancelled
    assert c.post(f"/api/sales/{cancelled}/cancel").status_code == 200
    expense = c.post(
        "/api/expenses", json={"category": "Rent", "description": "Shop rent", "amount": 300}
    )
    assert expense.status_code == 201, expense.text
    yield {"client": c, "rice": rice, "tea": tea}
    c.__exit__(None, None, None)


def _csv(client, type_: str = "consolidated") -> str:
    r = client.get("/api/reports/download", params={"type": type_, "preset": "month", "fmt": "csv"})
    assert r.status_code == 200, r.text
    return r.content.decode("utf-8-sig")


def _table(csv_text: str, title: str) -> list[list[str]]:
    """The rows under one titled table of the CSV, header included."""
    import csv
    import io

    rows = list(csv.reader(io.StringIO(csv_text)))
    start = rows.index([title])
    out = []
    for row in rows[start + 1 :]:
        if not row:
            break
        out.append(row)
    return out


def test_items_sold_counts_each_product_and_leaves_out_the_cancelled_sale(shop):
    table = _table(_csv(shop["client"]), "Items sold")
    assert table[0] == ["Item", "Sold", "Unit", "Sales value", "Share %"]
    # Rice 10 + 20 at 50; tea 2 at 400. The cancelled 5 kg of tea is not there.
    assert table[1:] == [
        ["Rice", "30", "kg", "1,500.00", "65.2"],
        ["Tea", "2", "kg", "800.00", "34.8"],
    ]


def test_the_profit_is_worked_out_from_sales_net_of_tax_cost_and_expenses(shop):
    text = _csv(shop["client"])
    summary = dict(row for row in __import__("csv").reader(text.splitlines()) if len(row) == 2)
    # Net of tax 2300; cost 30*40 + 2*300 = 1800; gross 500; less rent 300.
    assert summary["Sales net of tax"] == "INR 2,300.00"
    assert summary["Cost of goods sold"] == "INR 1,800.00"
    assert summary["Gross profit"] == "INR 500.00"
    assert summary["Net profit"] == "INR 200.00"
    assert "Net loss" not in summary


def test_the_sales_report_gives_gross_profit_and_no_expenses(shop):
    text = _csv(shop["client"], "sales")
    summary = dict(row for row in __import__("csv").reader(text.splitlines()) if len(row) == 2)
    assert summary["Gross profit"] == "INR 500.00"
    assert "Net profit" not in summary
    assert "Expenses" not in summary


def test_a_loss_is_called_a_loss(shop):
    c = shop["client"]
    big = c.post(
        "/api/expenses", json={"category": "Repairs", "description": "New shutter", "amount": 900}
    )
    assert big.status_code == 201, big.text
    try:
        text = _csv(c)
        summary = dict(row for row in __import__("csv").reader(text.splitlines()) if len(row) == 2)
        assert summary["Net loss"] == "INR 700.00"
        assert "Net profit" not in summary

        overview = c.get("/api/reports/overview").json()["totals"]
        assert overview["netProfit"] == pytest.approx(
            overview["grossProfit"] - overview["expenses"]
        )
        assert overview["netProfit"] == pytest.approx(-700)
    finally:
        assert c.delete(f"/api/expenses/{big.json()['id']}").status_code == 200


def test_stock_shows_a_new_product_and_a_manual_addition_at_once(shop):
    c = shop["client"]
    before = {row[0]: row for row in _table(_csv(c), "Stock on hand")[1:]}
    # 100 - 30 sold; the cancelled sale put its tea back: 20 - 2.
    assert before["Rice"][1:4] == ["70", "kg", "2,800.00"]
    assert before["Tea"][1:4] == ["18", "kg", "5,400.00"]

    # A product nobody has bought or sold yet, entered with stock on the shelf.
    new = c.post(
        "/api/products",
        json={
            "name": "Jaggery",
            "unit": "kg",
            "purchasePrice": 60,
            "sellingPrice": 75,
            "stock": 10,
        },
    )
    assert new.status_code == 201, new.text
    # And stock added by hand, with no purchase from a supplier behind it.
    adjusted = c.post(
        f"/api/products/{shop['rice']}/adjust",
        json={"delta": 30, "note": "Found in the store room"},
    )
    assert adjusted.status_code == 200, adjusted.text

    after = {row[0]: row for row in _table(_csv(c), "Stock on hand")[1:]}
    assert after["Jaggery"][1:4] == ["10", "kg", "600.00"]
    assert after["Rice"][1:4] == ["100", "kg", "4,000.00"]
    assert sum(float(row[4]) for row in after.values()) == pytest.approx(100, abs=0.2)


def test_a_product_with_no_purchase_price_still_has_a_slice(shop):
    c = shop["client"]
    made = c.post(
        "/api/products",
        json={"name": "Loose Sweets", "unit": "kg", "sellingPrice": 200, "stock": 3},
    )
    assert made.status_code == 201, made.text
    row = {r[0]: r for r in _table(_csv(c), "Stock on hand")[1:]}["Loose Sweets"]
    assert row[1:4] == ["3", "kg", "600.00"]


def test_the_pdf_is_a_pdf_with_the_charts_in_it(shop):
    c = shop["client"]
    for type_ in ("consolidated", "sales", "purchases", "expenses"):
        r = c.get("/api/reports/download", params={"type": type_, "preset": "month", "fmt": "pdf"})
        if type_ == "purchases":
            # No purchases recorded in this shop: nothing to download.
            assert r.status_code == 404
            continue
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(b"%PDF")


def test_the_expenses_report_has_no_charts_to_draw(shop):
    text = _csv(shop["client"], "expenses")
    assert "Items sold" not in text
    assert "Stock on hand" not in text


def test_many_products_still_make_a_report(anon):
    """More products than the chart has colours for, and more rows than fit on
    one page: the pie folds the tail and the full table runs on."""
    with _new_client() as c:
        r = c.post(
            "/api/auth/sign-up",
            json={
                "businessName": f"Big Catalogue {uuid.uuid4().hex[:6]}",
                "name": "Owner",
                "email": f"big-{uuid.uuid4().hex[:10]}@smoketest.dev",
                "password": "report-1234",
            },
        )
        assert r.status_code == 201, r.text
        items = []
        for n in range(70):
            made = c.post(
                "/api/products",
                json={
                    "name": f"Product number {n:02d} with a fairly long name",
                    "unit": "pcs",
                    "purchasePrice": 10 + n,
                    "sellingPrice": 15 + n,
                    "stock": 50,
                },
            )
            assert made.status_code == 201, made.text
            items.append(
                {
                    "productId": made.json()["id"],
                    "description": f"Product number {n:02d}",
                    "quantity": 1 + n % 3,
                    "unitPrice": 15 + n,
                }
            )
        sale = c.post("/api/sales", json={"items": items})
        assert sale.status_code == 201, sale.text

        pdf = c.get(
            "/api/reports/download",
            params={"type": "consolidated", "preset": "month", "fmt": "pdf"},
        )
        assert pdf.status_code == 200, pdf.text
        assert pdf.content.startswith(b"%PDF")
        table = _table(_csv(c), "Items sold")
        assert len(table) == 71
