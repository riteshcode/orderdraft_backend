"""
Tax invoice PDF (reportlab).
Australian tax invoice mein zaroori: "Tax Invoice" heading, seller ABN, date, items, GST amount.
"""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from xml.sax.saxutils import escape

from app.core.pdf_fonts import pdf_fonts
from app.models import Invoice, TenantSettings


def _m(value) -> str:
    return f"${float(value or 0):,.2f}"


def _e(text) -> str:
    """User ka text reportlab markup mein: & < > escape (warna "Fruit & Veg" pe PDF fail)."""
    return escape(str(text or ""))


def _br(text: str | None) -> str:
    return _e(text).replace("\n", "<br/>")


def invoice_pdf(invoice: Invoice, tenant: TenantSettings) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=f"Invoice {invoice.number}")
    styles = getSampleStyleSheet()
    c = invoice.customer
    font, bold = pdf_fonts([tenant.name, tenant.address, tenant.bank_details, tenant.invoice_footer, invoice.notes,
                            invoice.reference, c.name if c else "", c.address if c else ""]
                           + [l.description for l in invoice.lines])
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, leading=12, fontName=font)
    right = ParagraphStyle("right", parent=small, alignment=2)
    title = ParagraphStyle("title", parent=styles["Title"], alignment=2, fontSize=22, spaceAfter=0, fontName=bold)

    story = []

    # ---- Header: seller (left) + TAX INVOICE (right) ----
    seller = f"<b>{_e(tenant.name)}</b><br/>"
    if tenant.abn:
        seller += f"ABN {_e(tenant.abn)}<br/>"
    seller += _br(tenant.address)
    if tenant.phone:
        seller += f"<br/>{_e(tenant.phone)}"
    if tenant.email:
        seller += f"<br/>{_e(tenant.email)}"

    meta = (f"<b>Invoice #</b> {_e(invoice.number or '-')}<br/>"
            f"<b>Date</b> {invoice.issue_date.strftime('%d/%m/%Y')}<br/>")
    if invoice.due_date:
        meta += f"<b>Due</b> {invoice.due_date.strftime('%d/%m/%Y')}<br/>"
    if invoice.reference:
        meta += f"<b>Reference</b> {_e(invoice.reference)}"

    header = Table([[Paragraph(seller, small), [Paragraph("TAX INVOICE", title), Spacer(1, 4), Paragraph(meta, right)]]],
                   colWidths=[95 * mm, 79 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [header, Spacer(1, 10 * mm)]

    # ---- Bill to ----
    if c:
        bill = f"<b>Bill to</b><br/>{_e(c.name)}"
        if c.abn:
            bill += f"<br/>ABN {_e(c.abn)}"
        if c.address:
            bill += f"<br/>{_br(c.address)}"
        if c.email:
            bill += f"<br/>{_e(c.email)}"
        story += [Paragraph(bill, small), Spacer(1, 8 * mm)]

    # ---- Lines ----
    rows = [["Item", "Description", "Qty", "Unit price", "GST", "Amount"]]
    for l in invoice.lines:
        rows.append([l.sku or "", Paragraph(_e(l.description), small), f"{l.quantity:g}",
                     _m(l.unit_price), _m(l.gst_amount), _m(l.line_total)])
    table = Table(rows, colWidths=[26 * mm, 66 * mm, 14 * mm, 24 * mm, 20 * mm, 24 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [table, Spacer(1, 6 * mm)]

    # ---- Totals ----
    gst_label = "Includes GST" if tenant.prices_include_gst else f"GST ({tenant.gst_rate:g}%)"
    totals = [["Subtotal", _m(invoice.subtotal)], [gst_label, _m(invoice.gst)], ["Total AUD", _m(invoice.total)]]
    if float(invoice.amount_paid or 0) > 0:
        totals += [["Paid", _m(invoice.amount_paid)],
                   ["Balance due", _m(float(invoice.total) - float(invoice.amount_paid))]]
    t = Table(totals, colWidths=[40 * mm, 30 * mm], hAlign="RIGHT")
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTNAME", (0, 2), (-1, 2), bold),
        ("LINEABOVE", (0, 2), (-1, 2), 0.75, colors.black),
    ]))
    story += [t, Spacer(1, 10 * mm)]

    if invoice.status == "void":
        story.append(Paragraph("<font color='red'><b>VOID</b></font>", styles["Heading2"]))
    if tenant.bank_details:
        story += [Paragraph(f"<b>Payment details</b><br/>{_br(tenant.bank_details)}", small), Spacer(1, 5 * mm)]
    if invoice.notes:
        story += [Paragraph(f"<b>Notes</b><br/>{_br(invoice.notes)}", small), Spacer(1, 5 * mm)]
    if tenant.invoice_footer:
        story.append(Paragraph(_br(tenant.invoice_footer), small))

    doc.build(story)
    return buf.getvalue()
