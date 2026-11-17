"""Downloadable business reports (structured data, PDF and CSV).

Cancelled documents are listed but excluded from totals (the accountant-correct
behaviour). Amounts are printed as plain grouped numbers with the currency
stated once in the header, because the PDF core fonts have no rupee glyph.

Besides the document lists, a report says three things at a glance: whether
the period ended in profit or loss, what was sold (a share per product) and
what is on the shelf now. The last one is read from the products themselves,
so a product added yesterday, or stock corrected by hand with no purchase
behind it, is in the picture the moment it exists.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    CondPageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .core.utils import end_of_day, group_number, parse_date_input, round2, start_of_day
from .models import Business, Expense, Party, Product, Purchase, Sale, SaleItem

ReportType = str  # sales | purchases | expenses | consolidated
PeriodPreset = str

PERIOD_PRESETS = (
    "today",
    "week",
    "month",
    "last_month",
    "quarter",
    "half_year",
    "year",
    "last_12_months",
    "custom",
)

TITLES = {
    "sales": "Sales Report",
    "purchases": "Purchases Report",
    "expenses": "Expenses Report",
    "consolidated": "Consolidated Business Report",
}


@dataclass(slots=True)
class ReportSection:
    key: str
    title: str
    columns: list[str]
    #: Column indexes holding numbers (right-aligned in the PDF).
    numericColumns: list[int]
    rows: list[list[str]]
    count: int
    #: Net total, excluding cancelled documents.
    total: float
    cancelledCount: int


@dataclass(slots=True)
class BreakdownRow:
    name: str
    unit: str
    quantity: float
    amount: float
    #: Percent of the breakdown's total amount, 0 to 100.
    share: float


@dataclass(slots=True)
class Breakdown:
    """One whole split by product: what was sold, or what is in stock."""

    key: str  # itemsSold | stock
    title: str
    note: str
    quantityLabel: str
    amountLabel: str
    #: Largest amount first.
    rows: list[BreakdownRow]
    total: float


@dataclass(slots=True)
class ProfitFigure:
    """Whether the period made or lost money, and the sum behind it."""

    #: "Net profit", "Net loss", "Gross profit", "Gross loss" or "Break-even".
    label: str
    amount: float
    #: good | bad | even: what colour the figure is printed in.
    tone: str
    basis: str


@dataclass(slots=True)
class BusinessReport:
    business: dict
    title: str
    periodLabel: str
    rangeLabel: str
    generatedAt: str
    sections: list[ReportSection] = field(default_factory=list)
    summary: list[dict] = field(default_factory=list)
    fileName: str = "report.pdf"
    empty: bool = True
    profit: ProfitFigure | None = None
    breakdowns: list[Breakdown] = field(default_factory=list)


def _num(n: float) -> str:
    return group_number(n or 0)


def _qty(n: float) -> str:
    """A quantity without a trailing ".0": stock is counted in whole units."""
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


def _short_date(d: datetime) -> str:
    return d.strftime("%d %b %Y")


def resolve_period(
    preset: PeriodPreset, from_str: str | None = None, to_str: str | None = None
) -> tuple[datetime, datetime, str]:
    now = datetime.now()
    y, m = now.year, now.month

    if preset == "today":
        return start_of_day(now), end_of_day(now), "Today"
    if preset == "week":
        return start_of_day(now) - timedelta(days=6), end_of_day(now), "Last 7 days"
    if preset == "month":
        return datetime(y, m, 1), end_of_day(now), "This month"
    if preset == "last_month":
        first_this = datetime(y, m, 1)
        last_month_end = first_this - timedelta(days=1)
        return (
            datetime(last_month_end.year, last_month_end.month, 1),
            end_of_day(last_month_end),
            "Last month",
        )
    if preset == "quarter":
        return start_of_day(now) - timedelta(days=90), end_of_day(now), "Last 3 months"
    if preset == "half_year":
        return start_of_day(now) - timedelta(days=182), end_of_day(now), "Last 6 months"
    if preset == "year":
        return datetime(y, 1, 1), end_of_day(now), f"This year ({y})"
    if preset == "last_12_months":
        return start_of_day(now) - timedelta(days=365), end_of_day(now), "Last 12 months"

    # custom
    f = parse_date_input(from_str)
    t = parse_date_input(to_str)
    start = start_of_day(f) if f else datetime(y, m, 1)
    end = end_of_day(t) if t else end_of_day(now)
    # A backwards range would silently return nothing, so swap it instead.
    if start > end:
        start, end = start_of_day(end), end_of_day(start)
    return start, end, "Custom range"


def _with_shares(key, title, note, qty_label, amount_label, rows) -> Breakdown:
    rows = sorted(rows, key=lambda r: (-r.amount, r.name.lower()))
    total = round2(sum(r.amount for r in rows))
    for r in rows:
        r.share = round(r.amount / total * 100, 1) if total > 0 else 0.0
    return Breakdown(key, title, note, qty_label, amount_label, rows, total)


def items_sold(db: Session, business_id: uuid.UUID, start: datetime, end: datetime) -> Breakdown:
    """What was sold in the period, per product, by value.

    Grouped by the product a line points at, under its current name, so a
    product renamed half-way through the period is one slice and not two. A
    line typed free-hand with no product behind it is grouped by what it says.
    """
    grouped = db.execute(
        select(
            SaleItem.product_id,
            Product.name,
            Product.unit,
            SaleItem.description,
            func.sum(SaleItem.quantity),
            func.sum(SaleItem.line_total),
        )
        .select_from(SaleItem)
        .join(Sale, SaleItem.sale_id == Sale.id)
        .join(Product, SaleItem.product_id == Product.id, isouter=True)
        .where(
            Sale.business_id == business_id,
            Sale.status != "cancelled",
            Sale.date >= start,
            Sale.date <= end,
        )
        .group_by(SaleItem.product_id, Product.name, Product.unit, SaleItem.description)
    ).all()

    merged: dict[object, BreakdownRow] = {}
    for product_id, name, unit, description, quantity, amount in grouped:
        label = name or (description or "").strip() or "Unnamed item"
        key = product_id or label.lower()
        row = merged.setdefault(key, BreakdownRow(label, unit or "", 0, 0.0, 0.0))
        row.quantity += quantity or 0
        row.amount = round2(row.amount + (amount or 0))
    return _with_shares(
        "itemsSold",
        "Items sold",
        "Share of sales by product, before discount and tax. Cancelled sales are left out.",
        "Sold",
        "Sales value",
        list(merged.values()),
    )


def stock_on_hand(db: Session, business_id: uuid.UUID) -> Breakdown:
    """What is on the shelf right now, per product, by value.

    Read from the products, not rebuilt from purchases, so it includes a
    product that was only just added and any stock entered or corrected by
    hand. Valued at what the shop pays for it; a product with no purchase
    price yet falls back to its selling price, so it still has a slice.
    """
    products = db.scalars(
        select(Product).where(Product.business_id == business_id).order_by(Product.name)
    )
    rows = [
        BreakdownRow(
            p.name,
            p.unit or "",
            p.stock,
            round2(max(p.stock, 0) * (p.purchase_price or p.selling_price or 0)),
            0.0,
        )
        for p in products
    ]
    return _with_shares(
        "stock",
        "Stock on hand",
        "Every product as it stands now, whatever the period: new products and stock "
        "added or corrected by hand are included. Valued at purchase price.",
        "In stock",
        "Stock value",
        rows,
    )


def _profit_figure(net: float, *, gross_only: bool) -> ProfitFigure:
    kind = "Gross" if gross_only else "Net"
    basis = (
        "Sales net of tax, less what the goods sold cost to buy."
        if gross_only
        else "Sales net of tax, less what the goods sold cost to buy, less expenses."
    )
    if abs(net) < 0.005:
        return ProfitFigure("Break-even", 0.0, "even", basis)
    if net > 0:
        return ProfitFigure(f"{kind} profit", round2(net), "good", basis)
    return ProfitFigure(f"{kind} loss", round2(-net), "bad", basis)


def build_report(
    db: Session,
    business_id: uuid.UUID,
    *,
    type_: ReportType,
    preset: PeriodPreset,
    from_str: str | None = None,
    to_str: str | None = None,
) -> BusinessReport:
    start, end, label = resolve_period(preset, from_str, to_str)
    biz = db.scalar(select(Business).where(Business.id == business_id))
    if biz is None:
        raise ValueError("Business not found.")
    cur = biz.currency

    want_sales = type_ in ("sales", "consolidated")
    want_purchases = type_ in ("purchases", "consolidated")
    want_expenses = type_ in ("expenses", "consolidated")

    sections: list[ReportSection] = []
    sales_total = purchases_total = expenses_total = 0.0
    sales_tax = 0.0

    if want_sales:
        rows = list(
            db.execute(
                select(Sale, Party.name)
                .join(Party, Sale.party_id == Party.id, isouter=True)
                .where(Sale.business_id == business_id, Sale.date >= start, Sale.date <= end)
                .order_by(Sale.date.asc())
            ).all()
        )
        cancelled = 0
        body: list[list[str]] = []
        for sale, party_name in rows:
            if sale.status == "cancelled":
                cancelled += 1
            else:
                sales_total += sale.total
                sales_tax += sale.tax
            due = 0 if sale.status == "cancelled" else round2(sale.total - sale.amount_paid)
            body.append(
                [
                    _short_date(sale.date),
                    sale.invoice_number,
                    party_name or "Walk-in",
                    _num(sale.subtotal),
                    _num(sale.discount_amount),
                    _num(sale.tax),
                    _num(sale.total),
                    _num(sale.amount_paid),
                    _num(due),
                    "Cancelled" if sale.status == "cancelled" else sale.payment_status,
                ]
            )
        sales_total = round2(sales_total)
        sections.append(
            ReportSection(
                key="sales",
                title="Sales",
                columns=[
                    "Date",
                    "Invoice",
                    "Customer",
                    "Subtotal",
                    "Discount",
                    "Tax",
                    "Total",
                    "Paid",
                    "Due",
                    "Status",
                ],
                numericColumns=[3, 4, 5, 6, 7, 8],
                rows=body,
                count=len(rows),
                total=sales_total,
                cancelledCount=cancelled,
            )
        )

    if want_purchases:
        rows = list(
            db.execute(
                select(Purchase, Party.name)
                .join(Party, Purchase.party_id == Party.id, isouter=True)
                .where(
                    Purchase.business_id == business_id,
                    Purchase.date >= start,
                    Purchase.date <= end,
                )
                .order_by(Purchase.date.asc())
            ).all()
        )
        cancelled = 0
        body = []
        for pur, party_name in rows:
            if pur.status == "cancelled":
                cancelled += 1
            else:
                purchases_total += pur.total
            due = 0 if pur.status == "cancelled" else round2(pur.total - pur.amount_paid)
            body.append(
                [
                    _short_date(pur.date),
                    pur.reference_number,
                    party_name or "-",
                    _num(pur.subtotal),
                    _num(pur.discount_amount),
                    _num(pur.tax),
                    _num(pur.total),
                    _num(pur.amount_paid),
                    _num(due),
                    "Cancelled" if pur.status == "cancelled" else pur.payment_status,
                ]
            )
        purchases_total = round2(purchases_total)
        sections.append(
            ReportSection(
                key="purchases",
                title="Purchases",
                columns=[
                    "Date",
                    "Reference",
                    "Supplier",
                    "Subtotal",
                    "Discount",
                    "Tax",
                    "Total",
                    "Paid",
                    "Due",
                    "Status",
                ],
                numericColumns=[3, 4, 5, 6, 7, 8],
                rows=body,
                count=len(rows),
                total=purchases_total,
                cancelledCount=cancelled,
            )
        )

    if want_expenses:
        rows = list(
            db.scalars(
                select(Expense)
                .where(
                    Expense.business_id == business_id, Expense.date >= start, Expense.date <= end
                )
                .order_by(Expense.date.asc())
            )
        )
        expenses_total = round2(sum(e.amount for e in rows))
        sections.append(
            ReportSection(
                key="expenses",
                title="Expenses",
                columns=["Date", "Category", "Description", "Amount"],
                numericColumns=[3],
                rows=[
                    [_short_date(e.date), e.category, e.description, _num(e.amount)] for e in rows
                ],
                count=len(rows),
                total=expenses_total,
                cancelledCount=0,
            )
        )

    summary: list[dict] = []
    if want_sales:
        summary.append({"label": "Total sales", "value": f"{cur} {_num(sales_total)}"})
    if want_purchases:
        summary.append({"label": "Total purchases", "value": f"{cur} {_num(purchases_total)}"})
    if want_expenses:
        summary.append({"label": "Total expenses", "value": f"{cur} {_num(expenses_total)}"})
    if type_ == "consolidated":
        summary.append(
            {
                "label": "Net cash (sales - purchases - expenses)",
                "value": f"{cur} {_num(round2(sales_total - purchases_total - expenses_total))}",
                "strong": True,
            }
        )

    # Profit or loss. The same sum the dashboard's gross profit uses, over this
    # period: tax collected is owed onwards, not earned, and stock bought but
    # not yet sold is not a cost of the period, which is why this is not the
    # "net cash" line above.
    profit: ProfitFigure | None = None
    breakdowns: list[Breakdown] = []
    if want_sales:
        sales_net = round2(sales_total - sales_tax)
        cogs = round2(
            db.scalar(
                select(func.coalesce(func.sum(SaleItem.quantity * Product.purchase_price), 0.0))
                .select_from(SaleItem)
                .join(Sale, SaleItem.sale_id == Sale.id)
                .join(Product, SaleItem.product_id == Product.id)
                .where(
                    Sale.business_id == business_id,
                    Sale.status != "cancelled",
                    Sale.date >= start,
                    Sale.date <= end,
                )
            )
            or 0
        )
        gross = round2(sales_net - cogs)
        steps = [("Sales net of tax", sales_net), ("Cost of goods sold", cogs)]
        if type_ == "consolidated":
            steps += [("Gross profit", gross), ("Expenses", expenses_total)]
        for step, value in steps:
            summary.append({"label": step, "value": f"{cur} {_num(value)}", "group": "profit"})
        if type_ == "consolidated":
            profit = _profit_figure(round2(gross - expenses_total), gross_only=False)
        else:
            profit = _profit_figure(gross, gross_only=True)
        summary.append(
            {
                "label": profit.label,
                "value": f"{cur} {_num(profit.amount)}",
                "strong": True,
                "tone": profit.tone,
                "group": "profit",
            }
        )
        breakdowns.append(items_sold(db, business_id, start, end))
    if want_sales or want_purchases:
        breakdowns.append(stock_on_hand(db, business_id))

    for sec in sections:
        summary.append(
            {
                "label": f"{sec.title} recorded",
                "value": f"{sec.count} ({sec.cancelledCount} cancelled)"
                if sec.cancelledCount
                else str(sec.count),
            }
        )

    stamp = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
    slug = "".join(c if c.isalnum() else "-" for c in biz.name.lower()).strip("-") or "business"
    while "--" in slug:
        slug = slug.replace("--", "-")

    return BusinessReport(
        business={
            "name": biz.name,
            "address": biz.address,
            "gstNumber": biz.gst_number,
            "currency": cur,
        },
        title=TITLES.get(type_, "Business Report"),
        periodLabel=label,
        rangeLabel=f"{_short_date(start)} to {_short_date(end)}",
        generatedAt=datetime.now().strftime("%d %b %Y, %H:%M"),
        sections=sections,
        summary=summary,
        fileName=f"{slug}-{type_}-{stamp}.pdf",
        empty=all(not s.rows for s in sections),
        profit=profit,
        breakdowns=breakdowns,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
def render_csv(report: BusinessReport) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([report.business["name"], report.title])
    w.writerow([report.periodLabel, report.rangeLabel])
    w.writerow([f"Amounts in {report.business['currency']}"])
    w.writerow([])
    for s in report.summary:
        w.writerow([s["label"], s["value"]])
    for section in report.sections:
        w.writerow([])
        w.writerow([section.title])
        w.writerow(section.columns)
        for row in section.rows:
            w.writerow(row)
    for b in report.breakdowns:
        w.writerow([])
        w.writerow([b.title])
        w.writerow(["Item", b.quantityLabel, "Unit", b.amountLabel, "Share %"])
        for r in b.rows:
            w.writerow([r.name, _qty(r.quantity), r.unit, _num(r.amount), f"{r.share:.1f}"])
    return buf.getvalue().encode("utf-8-sig")


def _page_footer(canvas, doc, report: BusinessReport) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#8a8a8a"))
    width, _ = landscape(A4)
    canvas.drawString(15 * mm, 10 * mm, f"{report.business['name']} · {report.title}")
    canvas.drawRightString(width - 15 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


#: Slice colours, in a fixed order that keeps neighbours apart for the common
#: kinds of colour blindness as well. Identity never rests on colour alone:
#: every slice is named, with its figures, in the table beside the chart.
SLICE_COLOURS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4")
OTHER_COLOUR = "#b4b3ab"
#: Up to this many folded rows are named on the "Other" line itself; more than
#: that and every row is listed in a table under the chart instead.
FULL_LIST_AFTER = 3
#: tone -> (ink, tint): the profit or loss figure, and the band behind it.
TONE_COLOURS = {
    "good": ("#0b6e0b", "#e8f5e8"),
    "bad": ("#b42b2b", "#fbe9e9"),
    "even": ("#555555", "#f1f1f1"),
}


@dataclass(slots=True)
class Slice:
    label: str
    quantity: str
    amount: float
    share: float
    colour: str


def breakdown_slices(b: Breakdown) -> list[Slice]:
    """The pie's slices: the five largest, and the rest folded into one.

    More than a handful of slices cannot be told apart by eye, and a sixth
    colour would have to be invented. The full list is printed under the chart
    instead, so nothing is hidden by the folding.
    """
    live = [r for r in b.rows if r.amount > 0]
    head, tail = live[: len(SLICE_COLOURS)], live[len(SLICE_COLOURS) :]
    out = [
        Slice(r.name, f"{_qty(r.quantity)} {r.unit}".strip(), r.amount, r.share, SLICE_COLOURS[i])
        for i, r in enumerate(head)
    ]
    if tail:
        # A short tail is named in full, so "Other" hides nothing.
        if len(tail) == 1:
            label = tail[0].name
        elif len(tail) <= FULL_LIST_AFTER:
            label = "Other: " + ", ".join(r.name for r in tail)
        else:
            label = f"Other ({len(tail)} more items, listed below)"
        quantity = f"{_qty(tail[0].quantity)} {tail[0].unit}".strip() if len(tail) == 1 else ""
        out.append(
            Slice(
                label,
                quantity,
                round2(sum(r.amount for r in tail)),
                round(max(0.0, 100 - sum(s.share for s in out)), 1),
                OTHER_COLOUR,
            )
        )
    return out


def _pie(slices: list[Slice], size: float) -> Drawing:
    drawing = Drawing(size, size)
    pie = Pie()
    pie.x = pie.y = 2
    pie.width = pie.height = size - 4
    pie.data = [s.amount for s in slices]
    pie.labels = None
    pie.startAngle = 90
    pie.direction = "clockwise"
    # A white rule between neighbours, so two slices never merge into one shape.
    pie.slices.strokeColor = colors.white
    pie.slices.strokeWidth = 1.5
    for i, s in enumerate(slices):
        pie.slices[i].fillColor = colors.HexColor(s.colour)
    drawing.add(pie)
    return drawing


def _breakdown_flowables(b: Breakdown, width: float, h2, meta) -> list:
    """Title, note, the pie beside its legend, then every row as a table."""
    out: list = [Paragraph(b.title, h2), Paragraph(b.note, meta), Spacer(1, 6)]
    slices = breakdown_slices(b)
    if not slices:
        return [*out, Paragraph("Nothing to show for this period.", meta), Spacer(1, 10)]

    grid = colors.HexColor("#e2e2e8")
    # A Paragraph, so a long name (or a named "Other") wraps inside its column.
    cell = ParagraphStyle("legendcell", parent=meta, fontSize=8.5, leading=10.5)
    cell.textColor = colors.HexColor("#1a1a1a")
    legend_rows = [["", "Item", b.quantityLabel, b.amountLabel, "Share"]] + [
        ["", Paragraph(s.label, cell), s.quantity, _num(s.amount), f"{s.share:.1f}%"]
        for s in slices
    ]
    legend_rows.append(["", "Total", "", _num(b.total), "100.0%"])
    pie_width = 50 * mm
    legend_width = width - pie_width
    legend = Table(
        legend_rows,
        colWidths=[5 * mm, legend_width * 0.42, legend_width * 0.2, legend_width * 0.2, None],
        hAlign="LEFT",
    )
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#555555")),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, grid),
        ("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.HexColor("#b9b9c2")),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    for i, s in enumerate(slices, start=1):
        # The swatch: a coloured cell inset by white rules so it reads as a chip.
        style.append(("BACKGROUND", (0, i), (0, i), colors.HexColor(s.colour)))
        style.append(("BOX", (0, i), (0, i), 3, colors.white))
    legend.setStyle(TableStyle(style))

    side_by_side = Table(
        [[_pie(slices, pie_width - 5 * mm), legend]], colWidths=[pie_width, legend_width]
    )
    side_by_side.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    # Start a new page rather than part the chart from its title. The height is
    # an estimate on the generous side: title and note, then whichever is the
    # taller of the pie and its legend.
    needed = 50 + max(pie_width, 16.0 * len(legend_rows))
    out = [CondPageBreak(needed), *out, side_by_side, Spacer(1, 8)]

    # Every row, when the chart folded more of them together than it could name.
    if len(b.rows) > len(SLICE_COLOURS) + FULL_LIST_AFTER:
        data = [["#", "Item", b.quantityLabel, "Unit", b.amountLabel, "Share"]] + [
            [str(n), r.name, _qty(r.quantity), r.unit, _num(r.amount), f"{r.share:.1f}%"]
            for n, r in enumerate(b.rows, start=1)
        ]
        full = Table(data, repeatRows=1, hAlign="LEFT")
        full.setStyle(
            TableStyle(
                [
                    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f7")),
                    ("GRID", (0, 0), (-1, -1), 0.25, grid),
                    ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                    ("ALIGN", (4, 0), (5, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#fbfbfd")],
                    ),
                ]
            )
        )
        out += [full, Spacer(1, 8)]
    return [*out, Spacer(1, 6)]


def _profit_banner(profit: ProfitFigure, currency: str, width: float, base) -> Table:
    """Profit or loss in one coloured band: the word, the figure, the sum.

    The word carries the meaning and the colour repeats it, so the band still
    reads on a black-and-white printout.
    """
    ink, tint = (colors.HexColor(c) for c in TONE_COLOURS.get(profit.tone, TONE_COLOURS["even"]))
    word = ParagraphStyle("pfw", parent=base, fontSize=9, leading=12, textColor=ink)
    figure = ParagraphStyle("pff", parent=base, fontSize=15, leading=18, textColor=ink)
    basis = ParagraphStyle(
        "pfb", parent=base, fontSize=8.5, leading=11, textColor=colors.HexColor("#555555")
    )
    amount = "" if profit.tone == "even" else f"{currency} {_num(profit.amount)}"
    band = Table(
        [
            [
                [
                    Paragraph(f"<b>{profit.label.upper()}</b>", word),
                    Paragraph(f"<b>{amount or 'No profit, no loss'}</b>", figure),
                ],
                Paragraph(profit.basis, basis),
            ]
        ],
        colWidths=[width * 0.4, width * 0.6],
    )
    band.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), tint),
                ("LINEBEFORE", (0, 0), (0, -1), 3, ink),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return band


def render_pdf(report: BusinessReport) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title=report.title,
        author=report.business["name"],
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1x", parent=styles["Heading1"], fontSize=15, spaceAfter=2, leading=18)
    meta = ParagraphStyle(
        "metax",
        parent=styles["Normal"],
        fontSize=8.5,
        textColor=colors.HexColor("#666666"),
        leading=12,
    )
    meta_r = ParagraphStyle("metar", parent=meta, alignment=TA_RIGHT)
    h2 = ParagraphStyle("h2x", parent=styles["Heading2"], fontSize=11, spaceBefore=8, spaceAfter=4)

    story: list = []

    # Header: business on the left, report meta on the right.
    left = [Paragraph(report.business["name"], h1)]
    if report.business.get("address"):
        left.append(Paragraph(report.business["address"], meta))
    if report.business.get("gstNumber"):
        left.append(Paragraph(f"GSTIN: {report.business['gstNumber']}", meta))
    right = [
        Paragraph(f"<b>{report.title}</b>", meta_r),
        Paragraph(f"{report.periodLabel}: {report.rangeLabel}", meta_r),
        Paragraph(f"Generated {report.generatedAt}", meta_r),
        Paragraph(f"Amounts in {report.business['currency']}", meta_r),
    ]
    header = Table([[left, right]], colWidths=[doc.width * 0.55, doc.width * 0.45])
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("LINEBELOW", (0, 0), (-1, -1), 0.6, colors.HexColor("#d0d0d0")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story += [header, Spacer(1, 10)]

    # Profit or loss first: it is the question the report is opened to answer.
    if report.profit is not None:
        story += [
            _profit_banner(report.profit, report.business["currency"], doc.width, styles["Normal"]),
            Spacer(1, 6),
        ]

    # Summary
    story.append(Paragraph("Summary", h2))

    def summary_table(entries: list[dict]) -> Table:
        t = Table(
            [[s["label"], s["value"]] for s in entries],
            colWidths=[doc.width * 0.27, doc.width * 0.19],
            hAlign="LEFT",
        )
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
        ]
        for i, s in enumerate(entries):
            if s.get("tone") in TONE_COLOURS:
                ink = colors.HexColor(TONE_COLOURS[s["tone"]][0])
                style.append(("TEXTCOLOR", (0, i), (1, i), ink))
                style.append(("FONTNAME", (0, i), (0, i), "Helvetica-Bold"))
                style.append(("LINEABOVE", (0, i), (1, i), 0.6, colors.HexColor("#b9b9c2")))
        t.setStyle(TableStyle(style))
        return t

    # Totals on the left; the steps from sales down to profit on the right.
    totals = [s for s in report.summary if s.get("group") != "profit"]
    steps = [s for s in report.summary if s.get("group") == "profit"]
    if totals and steps:
        pair = Table(
            [[summary_table(totals), summary_table(steps)]],
            colWidths=[doc.width * 0.5, doc.width * 0.5],
        )
        pair.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        story.append(pair)
    elif report.summary:
        story.append(summary_table(report.summary))
    story.append(Spacer(1, 6))

    # What was sold and what is in stock, each as a chart with its figures.
    for breakdown in report.breakdowns:
        story += _breakdown_flowables(breakdown, doc.width, h2, meta)

    # Sections
    for section in report.sections:
        story.append(Paragraph(f"{section.title} ({section.count})", h2))
        if not section.rows:
            story += [Paragraph("No records in this period.", meta), Spacer(1, 8)]
            continue

        data = [section.columns, *section.rows]
        table = Table(data, repeatRows=1, hAlign="LEFT")
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f7")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#222222")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e2e8")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbfbfd")]),
        ]
        for col in section.numericColumns:
            style.append(("ALIGN", (col, 1), (col, -1), "RIGHT"))
        table.setStyle(TableStyle(style))
        story += [table, Spacer(1, 14)]

    doc.build(
        story,
        onFirstPage=lambda c, d: _page_footer(c, d, report),
        onLaterPages=lambda c, d: _page_footer(c, d, report),
    )
    return buf.getvalue()
