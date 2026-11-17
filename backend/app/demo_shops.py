"""Two provision stores to compare: one struggling, one healthy.

The first demo account shows what the app does. These two show what it is
for: the same kind of shop, the same catalogue and customers, four months of
trading each, run two different ways. One buys in small lots, discounts to
hold customers, collects late and pays late. The other buys in bulk, collects
inside each customer's terms and pays its suppliers on time. Side by side,
the dashboard, the health score and the reports tell them apart.

    python -m app.cli demo-shops            # load both
    python -m app.cli demo-shops --reset    # delete and load again
    python -m app.cli demo-shops --shop A   # only one of them

The records live in `app/data/demo_shops.txt`, written the way a shopkeeper
would enter them: a sentence per purchase, sale and expense, a line per
payment. They are loaded the way a person would enter them too. Every note is
read by the same offline parser Smart Input falls back to, grounded against
the shop's catalogue, checked, and published through the domain layer, so
stock, balances, alerts and events are the app's own work and not rows
written behind its back. Payments, cancellations, write-offs and the one
deadline change go through the functions their buttons call.

No AI key is used, whatever is configured: a demo that loads differently
depending on a rate limit is not a demo.

The dataset runs from 15 July to 11 November 2026. The health score looks at
the last 30 and 90 days from the moment it is viewed, so by default every date
is moved by the same number of days to make the last entry land on the day of
loading. `--as-dated` keeps the dates exactly as written.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .accounts import delete_business
from .ai.nlp import heuristic_parse
from .core.security import hash_password
from .core.utils import end_of_day, parse_date_input
from .domain import catalog, sales
from .domain.payments import record_payment
from .models import Business, Event, Notification, Party, Product, Purchase, Sale, User
from .schemas import PartyInput, ProductInput
from .seed import DEMO_SHOP_ACCOUNTS
from .smart_input import ground_text, publish_draft
from .worker import drain_queue
from .workflow import default_rules

DATASET = Path(__file__).parent / "data" / "demo_shops.txt"
#: The last day in the dataset. Loading moves this day onto the day of loading.
LAST_DAY = date(2026, 11, 11)
#: How many alerts are left unread, so the bell shows a few and not hundreds.
UNREAD_KEPT = 5

MONTHS = {
    name: number
    for number, name in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}


class DemoLoadError(RuntimeError):
    """A line of the dataset did not load the way it was written to."""


@dataclass(slots=True)
class Note:
    """A purchase, sale or expense as a sentence for Smart Input."""

    text: str
    day: date
    #: The number the app is expected to give it (sales and purchases).
    tag: str = ""
    paid: float = 0.0
    amount: float = 0.0


@dataclass(slots=True)
class PaymentLine:
    text: str
    amount: float
    doc: str
    mode: str
    reference: str | None
    day: date


@dataclass(slots=True)
class Operation:
    kind: str  # cancel | adjust | deadline
    text: str
    doc: str = ""
    product: str = ""
    note: str = ""
    delta: int = 0
    day: date | None = None


@dataclass(slots=True)
class ShopData:
    letter: str
    settings: dict = field(default_factory=dict)
    products: list[dict] = field(default_factory=list)
    parties: list[dict] = field(default_factory=list)
    purchases: list[Note] = field(default_factory=list)
    sales: list[Note] = field(default_factory=list)
    expenses: list[Note] = field(default_factory=list)
    payments_in: list[PaymentLine] = field(default_factory=list)
    payments_out: list[PaymentLine] = field(default_factory=list)
    operations: list[Operation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading the dataset
# ---------------------------------------------------------------------------
def _day(text: str) -> date:
    d, month, year = text.split()
    return date(int(year), MONTHS[month], int(d))


def _money(text: str) -> float:
    return float(text.replace(",", ""))


_PRODUCT = re.compile(
    r"^(.+?)\s+unit (\w+)\s+buy ₹\s*([\d,.]+)\s+sell ₹\s*([\d,.]+)\s+"
    r"opening\s+(\d+)\s+low-stock (\d+)$"
)
_PARTY = re.compile(r"^(.+?)\s{2,}(customer|supplier)\s+(\d+) days")
_SETTINGS = re.compile(r'^Settings: business name "(.+)", address "(.+)",$')
_SETTINGS_2 = re.compile(
    r"^tax rate ([\d.]+)%, invoice prefix (\w+), default payment terms (\d+) days\.$"
)
_NOTE = re.compile(r"^(.+\.)\s+\[([A-Z]+-\d{4})\]$")
_ON = re.compile(r" on (\d{1,2} [A-Z][a-z]+ \d{4})\.$")
_PAID = re.compile(r"paid ₹([\d,.]+)/-")
_EXPENSE = re.compile(r"^(?:Paid|Spent) .*₹([\d,.]+)/-.* on (\d{1,2} [A-Z][a-z]+ \d{4})\.$")
_PAYMENT = re.compile(
    r"^(?:Received|Paid) ₹([\d,.]+)(?: \((?:part payment|balance)\))? (?:from|to) (.+?) "
    r"against (?:invoice|purchase) ([A-Z]+-\d{4}) dated .+?, (.+), on "
    r"(\d{1,2} [A-Z][a-z]+ \d{4})\.$"
)
_CANCEL = re.compile(r"^Cancel invoice ([A-Z]+-\d{4})")
_WRITE_OFF = re.compile(
    r"^Write off \d+ \w+ of (.+?) on (\d{1,2} [A-Z][a-z]+ \d{4}): (.+)\. "
    r"\(Products page > Adjust, quantity (-\d+)\.\)$"
)
_DEADLINE = re.compile(
    r"^Extend the payment deadline of invoice ([A-Z]+-\d{4}) .* to "
    r"(\d{1,2} [A-Z][a-z]+ \d{4}):"
)


def _payment_mode(how: str) -> tuple[str, str | None]:
    if how == "in cash":
        return "cash", None
    if how.startswith("by UPI"):
        return "upi", re.search(r"transaction ID (\w+)", how)[1]
    if how.startswith("by cheque"):
        return "cheque", re.search(r"no\. (\w+)", how)[1]
    return "bank_transfer", re.search(r"UTR (\w+)", how)[1]


def read_shop(letter: str, path: Path = DATASET) -> ShopData:
    """One business's block of the dataset, section by section."""
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(f"BUSINESS {letter}:"))
    except StopIteration:
        raise DemoLoadError(f"There is no business {letter} in {path.name}.") from None
    end = next(
        (
            i
            for i, line in enumerate(lines)
            if i > start and (line.startswith("BUSINESS ") or line.startswith("END OF DATASET"))
        ),
        len(lines),
    )
    block = lines[start:end]

    shop = ShopData(letter)
    section: int | None = None
    for i, line in enumerate(block):
        heading = re.match(rf"^{letter}(\d)\. ", line)
        if heading:
            section = int(heading[1])
            continue
        s = line.strip()
        if not s or s.startswith("----") or s.startswith("===="):
            continue

        if section == 0:
            if m := _SETTINGS.match(s):
                rest = _SETTINGS_2.match(block[i + 1].strip())
                shop.settings = {
                    "name": m[1],
                    "address": m[2],
                    "taxRate": float(rest[1]),
                    "invoicePrefix": rest[2],
                    "creditDays": int(rest[3]),
                }
            elif m := _PRODUCT.match(s):
                shop.products.append(
                    {
                        "name": m[1],
                        "unit": m[2],
                        "purchasePrice": _money(m[3]),
                        "sellingPrice": _money(m[4]),
                        "stock": int(m[5]),
                        "lowStockThreshold": int(m[6]),
                    }
                )
            elif m := _PARTY.match(s):
                shop.parties.append({"name": m[1], "type": m[2], "creditDays": int(m[3])})
        elif section in (1, 2):
            if m := _NOTE.match(s):
                text = m[1]
                paid = _PAID.search(text)
                note = Note(
                    text=text,
                    day=_day(_ON.search(text)[1]),
                    tag=m[2],
                    paid=_money(paid[1]) if paid else 0.0,
                )
                (shop.purchases if section == 1 else shop.sales).append(note)
        elif section == 3:
            if m := _EXPENSE.match(s):
                shop.expenses.append(Note(text=s, day=_day(m[2]), amount=_money(m[1])))
        elif section in (4, 5):
            if m := _PAYMENT.match(s):
                mode, reference = _payment_mode(m[4])
                row = PaymentLine(s, _money(m[1]), m[3], mode, reference, _day(m[5]))
                (shop.payments_in if section == 4 else shop.payments_out).append(row)
        elif section == 6:
            if m := _CANCEL.match(s):
                shop.operations.append(Operation("cancel", s, doc=m[1]))
            elif m := _WRITE_OFF.match(s):
                shop.operations.append(
                    Operation("adjust", s, product=m[1], day=_day(m[2]), note=m[3], delta=int(m[4]))
                )
            elif m := _DEADLINE.match(s):
                shop.operations.append(Operation("deadline", s, doc=m[1], day=_day(m[2])))
    if not shop.settings:
        raise DemoLoadError(f"Business {letter} has no Settings line in {path.name}.")
    return shop


# ---------------------------------------------------------------------------
# Drafting: the note through the offline parser, then checked
# ---------------------------------------------------------------------------
def draft_note(note: Note, kind: str, parties: list, products: list) -> dict:
    """The payload Smart Input would publish for one note, or a DemoLoadError
    naming exactly what the parser read differently from what was written.

    This is the whole reason the dataset can be trusted to load: a note that
    stops producing the intended draft fails here, by name, instead of going
    in as something else.
    """
    parsed = heuristic_parse(note.text)
    parsed.engine = "Heuristic"
    draft = ground_text(parsed, parties, products, note=note.text)

    problems: list[str] = []
    if draft["suggestedType"] != kind:
        problems.append(f"read as a {draft['suggestedType']}, expected a {kind}")
    if draft.get("date") != note.day.isoformat():
        problems.append(f"date read as {draft.get('date')}, expected {note.day.isoformat()}")

    if kind == "expense":
        if abs((draft.get("amount") or 0) - note.amount) > 0.005:
            problems.append(f"amount read as {draft.get('amount')}, expected {note.amount}")
        payload = {
            "type": "expense",
            "source": "nlp",
            "category": draft["category"],
            "description": draft["note"],
            "amount": draft["amount"],
        }
    else:
        walk_in = "walk-in customer" in note.text or note.text.startswith("A customer ")
        if not draft["partyId"] and not walk_in:
            problems.append(f"party {draft['partyName']!r} not matched to a saved party")
        if draft["partyId"] and walk_in:
            problems.append(f"a walk-in sale was matched to {draft['partyName']!r}")
        unmatched = [i["description"] for i in draft["items"] if not i["productId"]]
        if unmatched:
            problems.append(f"products not matched: {unmatched}")
        if not draft["items"]:
            problems.append("no line items read")
        payload = {
            "type": kind,
            "source": "nlp",
            "partyId": draft["partyId"],
            "items": draft["items"],
            "amountPaid": note.paid,
            "discountType": draft["discountType"],
            "discountValue": draft["discountValue"],
        }
    if problems:
        raise DemoLoadError(f"{note.text}\n  -> " + "; ".join(problems))
    return payload


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class LoadResult:
    letter: str
    business_id: uuid.UUID
    name: str
    email: str
    password: str
    counts: dict[str, int]
    shift_days: int


def existing_shop(db: Session, letter: str) -> Business | None:
    """The business a demo shop's owner account belongs to, if it is loaded."""
    owner = db.scalar(select(User).where(User.email == DEMO_SHOP_ACCOUNTS[letter]["email"]))
    if owner is None or owner.business_id is None:
        return None
    return db.scalar(select(Business).where(Business.id == owner.business_id))


def remove_shop(db: Session, letter: str) -> bool:
    business = existing_shop(db, letter)
    if business is None:
        return False
    delete_business(db, business.id)
    return True


def load_shop(
    db: Session,
    letter: str,
    *,
    end_on: date | None = None,
    as_dated: bool = False,
    path: Path = DATASET,
    progress: Callable[[str], None] | None = None,
) -> LoadResult:
    """Create one demo shop and enter its whole history.

    Refuses if the shop is already there; `remove_shop` first to reload. If a
    line fails, the half-loaded business is removed again, so a failed load
    never leaves a shop that looks finished and is not.
    """
    say = progress or (lambda _msg: None)
    account = DEMO_SHOP_ACCOUNTS[letter]
    if db.scalar(select(User.id).where(User.email == account["email"])):
        raise DemoLoadError(
            f"Demo shop {letter} is already loaded ({account['email']}). "
            "Run with --reset to delete it and load it again."
        )

    shop = read_shop(letter, path)
    shift = timedelta(0) if as_dated else (end_on or date.today()) - LAST_DAY

    def when(day: date) -> str:
        return (day + shift).isoformat()

    business = Business(
        name=shop.settings["name"],
        address=shop.settings["address"],
        tax_rate=shop.settings["taxRate"],
        invoice_prefix=shop.settings["invoicePrefix"],
        credit_days=shop.settings["creditDays"],
        currency="INR",
    )
    db.add(business)
    db.flush()
    owner = User(
        business_id=business.id,
        email=account["email"],
        name=account["name"],
        password_hash=hash_password(account["password"]),
        role="owner",
    )
    db.add(owner)
    db.add_all(default_rules(business.id))
    db.commit()
    bid, actor = business.id, owner.id

    def fresh() -> None:
        """Forget everything this session has loaded.

        The app gives each request a session of its own. Here one session
        enters hundreds of records, and between them the event worker changes
        stock and balances from a session of its own. Without this, the next
        step would read a party's balance as it was an hour of history ago and
        write its sum on top of the real one.
        """
        db.expunge_all()

    try:
        for p in shop.products:
            catalog.create_product(db, bid, ProductInput(**p))
        for p in shop.parties:
            catalog.create_party(db, bid, PartyInput(**p))
        # The catalogue the notes are grounded against: names and prices, which
        # do not change during the load. Detached below, so it stays as read.
        parties = list(db.scalars(select(Party).where(Party.business_id == bid)))
        products = list(db.scalars(select(Product).where(Product.business_id == bid)))
        fresh()
        say(f"  setup: {len(products)} products, {len(parties)} parties")

        # Every purchase before any sale: stock only ever runs higher than it
        # did on the day, so no sale is refused for stock that arrived "later",
        # and the invoice numbers still come out in date order.
        for label, kind, notes in (
            ("purchases", "purchase", shop.purchases),
            ("sales", "sale", shop.sales),
            ("expenses", "expense", shop.expenses),
        ):
            for n, note in enumerate(notes, start=1):
                payload = draft_note(note, kind, parties, products)
                payload["date"] = when(note.day)
                fresh()
                result = publish_draft(db, bid, payload, actor)
                _apply_events(db, bid)
                if note.tag and note.tag not in result.get("ok", ""):
                    raise DemoLoadError(
                        f"{note.text}\n  -> numbered {result.get('ok')!r}, expected {note.tag}"
                    )
                if n % 25 == 0 or n == len(notes):
                    say(f"  {label}: {n}/{len(notes)}")

        sale_ids = dict(
            db.execute(select(Sale.invoice_number, Sale.id).where(Sale.business_id == bid)).all()
        )
        purchase_ids = dict(
            db.execute(
                select(Purchase.reference_number, Purchase.id).where(Purchase.business_id == bid)
            ).all()
        )
        for label, rows, ids, key in (
            ("customer payments", shop.payments_in, sale_ids, "sale_id"),
            ("supplier payments", shop.payments_out, purchase_ids, "purchase_id"),
        ):
            for row in rows:
                if row.doc not in ids:
                    raise DemoLoadError(f"{row.text}\n  -> there is no {row.doc} to pay against")
                fresh()
                try:
                    record_payment(
                        db,
                        bid,
                        **{key: ids[row.doc]},
                        amount=row.amount,
                        date=parse_date_input(when(row.day)),
                        mode=row.mode,
                        reference=row.reference,
                    )
                except ValueError as err:
                    raise DemoLoadError(f"{row.text}\n  -> {err}") from err
            say(f"  {label}: {len(rows)}")

        by_name = {p.name: p.id for p in products}
        for op in shop.operations:
            fresh()
            try:
                if op.kind == "cancel":
                    sales.cancel_sale(db, bid, sale_ids[op.doc])
                elif op.kind == "adjust":
                    catalog.adjust_stock(db, bid, by_name[op.product], op.delta, op.note, actor)
                else:
                    due = end_of_day(parse_date_input(when(op.day)))
                    sales.update_sale_due_date(db, bid, sale_ids[op.doc], due)
            except (ValueError, KeyError) as err:
                raise DemoLoadError(f"{op.text}\n  -> {err}") from err
            _apply_events(db, bid)
        say(f"  cancellations, write-offs and deadline changes: {len(shop.operations)}")

        fresh()
        _settle_the_bell(db, bid)
    except Exception:
        db.rollback()
        delete_business(db, bid)
        raise

    return LoadResult(
        letter=letter,
        business_id=bid,
        name=shop.settings["name"],
        email=account["email"],
        password=account["password"],
        counts={
            "purchases": len(shop.purchases),
            "sales": len(shop.sales),
            "expenses": len(shop.expenses),
            "customer payments": len(shop.payments_in),
            "supplier payments": len(shop.payments_out),
        },
        shift_days=shift.days,
    )


def _apply_events(db: Session, business_id: uuid.UUID, timeout: float = 120.0) -> None:
    """Apply the event chain of what was just written, and wait until it is.

    Draining is normally enough. But loading into a database a running API is
    also serving means its worker may have claimed an event first, and the
    next record must not be entered while stock or a balance is still on its
    way: the sale after a purchase needs that purchase's stock.
    """
    deadline = time.monotonic() + timeout
    while True:
        drain_queue()
        states = dict(
            db.execute(
                select(Event.status, func.count())
                .where(
                    Event.business_id == business_id,
                    Event.status.in_(("pending", "processing", "dead")),
                )
                .group_by(Event.status)
            ).all()
        )
        if states.get("dead"):
            raise DemoLoadError("An event failed for good while loading; see the Event bus page.")
        if not states:
            return
        if time.monotonic() > deadline:
            raise DemoLoadError("Events are not being applied; is a worker stuck?")
        time.sleep(0.1)


def _settle_the_bell(db: Session, business_id: uuid.UUID) -> None:
    """Mark all but the newest few alerts read.

    Four months entered in a minute raise four months of alerts at once. A
    shop that had really been running would have read them as they came.
    """
    keep = list(
        db.scalars(
            select(Notification.id)
            .where(Notification.business_id == business_id)
            .order_by(Notification.created_at.desc(), Notification.id)
            .limit(UNREAD_KEPT)
        )
    )
    stmt = update(Notification).where(Notification.business_id == business_id)
    if keep:
        stmt = stmt.where(Notification.id.notin_(keep))
    db.execute(stmt.values(read=True))
    db.commit()
