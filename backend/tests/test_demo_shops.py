"""The two comparison shops: reading the dataset, and loading it.

The dataset is entered through the offline parser, so the first thing worth
knowing is that every note in it still drafts as written: that is checked for
all 448 of them without a database. The loader itself is exercised on a small
shop in the same format, where every number can be worked out by hand.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.utils import round2
from app.demo_shops import (
    LAST_DAY,
    DemoLoadError,
    Note,
    draft_note,
    existing_shop,
    load_shop,
    read_shop,
    remove_shop,
)
from tests.conftest import _new_client

EXPECTED = {
    "A": {
        "name": "Sri Annapoorneshwari Provision Stores",
        "prefix": "SAP",
        "products": 24,
        "parties": 10,
        "purchases": 41,
        "sales": 115,
        "expenses": 69,
        "payments_in": 67,
        "payments_out": 30,
        "operations": 5,
    },
    "B": {
        "name": "Sri Lakshmi Provision Stores",
        "prefix": "SLP",
        "products": 24,
        "parties": 12,
        "purchases": 38,
        "sales": 144,
        "expenses": 41,
        "payments_in": 53,
        "payments_out": 34,
        "operations": 2,
    },
}


# ---------------------------------------------------------------------------
# The dataset as shipped
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("letter", ["A", "B"])
def test_every_section_of_the_dataset_is_read(letter):
    shop = read_shop(letter)
    want = EXPECTED[letter]
    assert shop.settings == {
        "name": want["name"],
        "address": shop.settings["address"],
        "taxRate": 5.0,
        "invoicePrefix": want["prefix"],
        "creditDays": 30,
    }
    for section in (
        "products",
        "parties",
        "purchases",
        "sales",
        "expenses",
        "payments_in",
        "payments_out",
        "operations",
    ):
        assert len(getattr(shop, section)) == want[section], section
    # Numbered in the order they will be entered, with no gaps.
    assert [n.tag for n in shop.sales] == [
        f"{want['prefix']}-{i:04d}" for i in range(1, want["sales"] + 1)
    ]
    assert [n.tag for n in shop.purchases] == [
        f"PO-{i:04d}" for i in range(1, want["purchases"] + 1)
    ]
    # Four months, ending on the dataset's last day: the day the loader moves
    # onto the day of loading. Nothing may be dated after it.
    days = [n.day for n in shop.sales + shop.purchases + shop.expenses]
    days += [p.day for p in shop.payments_in + shop.payments_out]
    assert min(days) == date(2026, 7, 15)
    assert max(days) == LAST_DAY == date(2026, 11, 11)
    assert {p.mode for p in shop.payments_in + shop.payments_out} <= {
        "cash",
        "upi",
        "cheque",
        "bank_transfer",
    }
    assert all(p.reference for p in shop.payments_in if p.mode != "cash")


def _catalogue(shop) -> tuple[list, list]:
    """The shop's parties and products as the matcher sees them, without a
    database: the same shape the accuracy harness uses."""
    parties = [
        SimpleNamespace(id=str(uuid.uuid4()), name=p["name"], type=p["type"], phone=None)
        for p in shop.parties
    ]
    products = [
        SimpleNamespace(
            id=str(uuid.uuid4()),
            name=p["name"],
            unit=p["unit"],
            selling_price=p["sellingPrice"],
            purchase_price=p["purchasePrice"],
            stock=p["stock"],
        )
        for p in shop.products
    ]
    return parties, products


@pytest.mark.parametrize("letter", ["A", "B"])
def test_every_note_in_the_dataset_drafts_as_it_was_written(letter):
    """All of them, through the parser the loader uses. A change to the parser
    that makes one of these read differently fails here, by name, long before
    anyone loads a demo."""
    shop = read_shop(letter)
    parties, products = _catalogue(shop)
    by_id = {p.id: p for p in parties}
    tax = 1 + shop.settings["taxRate"] / 100

    for kind, notes in (
        ("purchase", shop.purchases),
        ("sale", shop.sales),
        ("expense", shop.expenses),
    ):
        for note in notes:
            payload = draft_note(note, kind, parties, products)  # raises on any mismatch
            if kind == "expense":
                assert payload["amount"] == note.amount
                assert payload["category"]
                continue
            assert payload["items"], note.text
            assert all(i["quantity"] > 0 and i["unitPrice"] > 0 for i in payload["items"])
            if payload["partyId"]:
                wanted = "supplier" if kind == "purchase" else "customer"
                assert by_id[payload["partyId"]].type == wanted, note.text
            # A counter sale is paid in full on the spot, so the amount in the
            # sentence is the bill: the quantities and prices read from it have
            # to add up to exactly that.
            if kind == "sale" and not payload["partyId"]:
                subtotal = sum(i["quantity"] * i["unitPrice"] for i in payload["items"])
                assert round2(subtotal * tax) == note.paid, note.text


def test_a_note_that_reads_differently_is_refused_by_name():
    shop = read_shop("A")
    parties, products = _catalogue(shop)

    unknown = Note(
        "Sold 3 kg unobtainium to a walk-in customer on 15 July 2026.", date(2026, 7, 15)
    )
    with pytest.raises(DemoLoadError, match="unobtainium"):
        draft_note(unknown, "sale", parties, products)

    wrong_day = Note(
        "Sold 3 kg sugar to a walk-in customer on 15 July 2026.", date(2026, 7, 16), paid=1
    )
    with pytest.raises(DemoLoadError, match="date read as 2026-07-15"):
        draft_note(wrong_day, "sale", parties, products)

    wrong_kind = Note("Sold 3 kg sugar to a walk-in customer on 15 July 2026.", date(2026, 7, 15))
    with pytest.raises(DemoLoadError, match="expected a purchase"):
        draft_note(wrong_kind, "purchase", parties, products)


def test_an_unknown_business_is_an_error_not_an_empty_shop():
    with pytest.raises(DemoLoadError, match="no business Z"):
        read_shop("Z")


# ---------------------------------------------------------------------------
# Loading, on a small shop in the same format
# ---------------------------------------------------------------------------
#: A two-product shop in the dataset's own format, small enough to check by hand.
MINI = (Path(__file__).parent / "fixtures" / "mini_shops.txt").read_text(encoding="utf-8")


@pytest.fixture
def mini(tmp_path, anon):
    """The small shop on disk, and a database with no shop A in it before or after."""
    from app.core.db import SessionLocal

    path = tmp_path / "mini_shops.txt"
    path.write_text(MINI, encoding="utf-8")
    with SessionLocal() as db:
        remove_shop(db, "A")
        yield db, path
        db.rollback()
        remove_shop(db, "A")


def test_a_shop_loads_through_the_app_and_every_figure_is_right(mini):
    from app.models import Expense, Notification, Party, Payment, Product, Purchase, Sale
    from app.seed import DEMO_SHOP_ACCOUNTS

    db, path = mini
    said: list[str] = []
    result = load_shop(db, "A", as_dated=True, path=path, progress=said.append)
    bid = result.business_id
    assert result.name == "Mini Test Stores"
    assert result.shift_days == 0
    assert result.counts == {
        "purchases": 1,
        "sales": 3,
        "expenses": 1,
        "customer payments": 1,
        "supplier payments": 1,
    }
    assert any("sales: 3/3" in line for line in said)

    db.expunge_all()
    stock = dict(
        db.execute(select(Product.name, Product.stock).where(Product.business_id == bid)).all()
    )
    # Sugar: 100 opening + 50 bought - 20 - 10 sold + 10 back from the
    # cancelled sale - 5 written off. Tea: 10 - 2 - 1.
    assert stock == {"Sugar 1kg": 125, "Tea Powder 1kg": 7}

    sales = {s.invoice_number: s for s in db.scalars(select(Sale).where(Sale.business_id == bid))}
    first = sales["MTS-0001"]
    assert (first.total, first.amount_paid, first.payment_status) == (2100.0, 1000.0, "partial")
    assert first.date.date() == date(2026, 11, 3)
    assert first.due_date.date() == date(2026, 11, 30)  # extended from the 15-day terms
    assert first.source == "nlp"
    counter = sales["MTS-0002"]
    assert (counter.total, counter.amount_paid, counter.payment_status) == (525.0, 525.0, "paid")
    assert counter.party_id is None
    assert sales["MTS-0003"].status == "cancelled"

    purchase = db.scalar(select(Purchase).where(Purchase.business_id == bid))
    assert (purchase.reference_number, purchase.total, purchase.payment_status) == (
        "PO-0001",
        2100.0,
        "paid",
    )

    balances = dict(
        db.execute(select(Party.name, Party.balance).where(Party.business_id == bid)).all()
    )
    # The cancelled sale is off the customer's account; the supplier is settled.
    assert balances == {"Asha Tiffin Room": 1100.0, "Balaji Wholesale Mart": 0.0}

    payments = {
        (p.mode, p.reference, p.amount, p.date.date())
        for p in db.scalars(select(Payment).where(Payment.business_id == bid))
    }
    assert ("cash", None, 1000.0, date(2026, 11, 7)) in payments
    assert ("upi", "123456789012", 2100.0, date(2026, 11, 8)) in payments

    expense = db.scalar(select(Expense).where(Expense.business_id == bid))
    assert (expense.amount, expense.category, expense.date.date()) == (
        5000.0,
        "Rent",
        date(2026, 11, 6),
    )

    unread = db.scalars(
        select(Notification.id).where(Notification.business_id == bid, Notification.read.is_(False))
    ).all()
    assert len(unread) <= 5

    # The owner can sign in, and the sign-in page is told the shop is there.
    account = DEMO_SHOP_ACCOUNTS["A"]
    with _new_client() as c:
        listed = c.get("/api/auth/demo-accounts").json()["accounts"]
        entry = next(a for a in listed if a["key"] == "A")
        assert entry["business"] == "Mini Test Stores"
        assert entry["email"] == account["email"]
        signed_in = c.post(
            "/api/auth/sign-in", json={"email": account["email"], "password": account["password"]}
        )
        assert signed_in.status_code == 200, signed_in.text
        dashboard = c.get("/api/dashboard")
        assert dashboard.status_code == 200
        assert dashboard.json()["business"]["name"] == "Mini Test Stores"
        # A shared demo account cannot be deleted by whoever signs into it.
        refused = c.post("/api/auth/delete-account", json={"password": account["password"]})
        assert refused.status_code == 403


def test_dates_move_together_so_the_last_entry_lands_on_the_chosen_day(mini):
    from app.models import Expense, Payment, Sale

    db, path = mini
    # The dataset's last day is 11 November 2026; land it ten days earlier.
    result = load_shop(db, "A", end_on=date(2026, 11, 1), path=path)
    assert result.shift_days == -10
    bid = result.business_id
    db.expunge_all()

    first = db.scalar(
        select(Sale).where(Sale.business_id == bid, Sale.invoice_number == "MTS-0001")
    )
    assert first.date.date() == date(2026, 10, 24)
    assert first.due_date.date() == date(2026, 11, 20)
    assert db.scalar(select(Expense.date).where(Expense.business_id == bid)).date() == date(
        2026, 10, 27
    )
    paid_on = {p.date.date() for p in db.scalars(select(Payment).where(Payment.business_id == bid))}
    # 7 and 8 November, and the counter sale paid on its own day (4 November).
    assert paid_on == {date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 25)}


def test_loading_twice_is_refused_and_removing_takes_everything(mini):
    from app.models import Business, Sale, User
    from app.seed import DEMO_SHOP_ACCOUNTS

    db, path = mini
    result = load_shop(db, "A", as_dated=True, path=path)
    with pytest.raises(DemoLoadError, match="already loaded"):
        load_shop(db, "A", as_dated=True, path=path)
    assert existing_shop(db, "A").id == result.business_id

    assert remove_shop(db, "A") is True
    db.expunge_all()
    assert existing_shop(db, "A") is None
    assert db.scalar(select(Business.id).where(Business.id == result.business_id)) is None
    assert db.scalar(select(Sale.id).where(Sale.business_id == result.business_id)) is None
    assert db.scalar(select(User.id).where(User.email == DEMO_SHOP_ACCOUNTS["A"]["email"])) is None
    assert remove_shop(db, "A") is False


def test_a_line_that_fails_leaves_no_half_loaded_shop(mini):
    from app.models import Business

    db, path = mini
    broken = MINI.replace(
        "against invoice MTS-0001 dated 3 November 2026, in cash",
        "against invoice MTS-0099 dated 3 November 2026, in cash",
    )
    assert broken != MINI
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(DemoLoadError, match="MTS-0099"):
        load_shop(db, "A", as_dated=True, path=path)
    db.expunge_all()
    assert existing_shop(db, "A") is None
    assert db.scalar(select(Business.id).where(Business.name == "Mini Test Stores")) is None


def test_the_sign_in_page_is_only_offered_accounts_that_exist(mini):
    # The fixture guarantees shop A is not loaded.
    with _new_client() as c:
        r = c.get("/api/auth/demo-accounts")
        assert r.status_code == 200
        assert "A" not in {a["key"] for a in r.json()["accounts"]}
