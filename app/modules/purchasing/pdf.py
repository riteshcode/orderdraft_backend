"""Purchase order PDF (reportlab) - supplier ko email hota hai."""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

from app.core.pdf_fonts import pdf_fonts
from app.models import PurchaseOrder, TenantSettings


def _m(value) -> str:
    return f"${float(value or 0):,.2f}"


def _txt(text: str | None) -> str:
    return escape(text or "").replace("\n", "<br/>")


def _qty(q: float) -> str:
    return f"{q:g}"


def po_pdf(po: PurchaseOrder, cfg: TenantSettings) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=f"Purchase order {po.number}")
    styles = getSampleStyleSheet()
    font, bold = pdf_fonts([cfg.business_name, cfg.address, po.notes, po.deliver_to, po.reference, po.supplier.name,
                            po.supplier.address, po.supplier.contact_name] + [l.description for l in po.lines])
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, leading=12, fontName=font)
    title = ParagraphStyle("title", parent=styles["Title"], alignment=2, fontSize=20, spaceAfter=0, fontName=bold)
    story = []

    buyer = f"<b>{_txt(cfg.business_name)}</b><br/>"
    if cfg.abn:
        buyer += f"ABN {_txt(cfg.abn)}<br/>"
    buyer += _txt(cfg.address)
    if cfg.phone:
        buyer += f"<br/>{_txt(cfg.phone)}"
    if cfg.email:
        buyer += f"<br/>{_txt(cfg.email)}"
    head = Table([[Paragraph(buyer, small), Paragraph("PURCHASE ORDER", title)]], colWidths=[95 * mm, 79 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 8 * mm)]

    s = po.supplier
    supplier = f"<b>Supplier</b><br/>{_txt(s.name)}"
    if s.contact_name:
        supplier += f"<br/>Attn: {_txt(s.contact_name)}"
    if s.address:
        supplier += f"<br/>{_txt(s.address)}"
    if s.email:
        supplier += f"<br/>{_txt(s.email)}"
    meta = [["PO number", po.number], ["Order date", po.order_date.strftime("%d/%m/%Y")]]
    if po.expected_date:
        meta.append(["Delivery by", po.expected_date.strftime("%d/%m/%Y")])
    if po.reference:
        meta.append(["Reference", po.reference])
    if s.account_number:
        meta.append(["Our account", s.account_number])
    meta_t = Table([[Paragraph(f"<b>{k}</b>", small), Paragraph(_txt(v), small)] for k, v in meta],
                   colWidths=[28 * mm, 50 * mm])
    deliver = f"<b>Deliver to</b><br/>{_txt(po.deliver_to or cfg.address or cfg.business_name)}"
    top = Table([[Paragraph(supplier, small), Paragraph(deliver, small), meta_t]], colWidths=[62 * mm, 52 * mm, 60 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [top, Spacer(1, 8 * mm)]

    rows = [["Item", "Supplier code", "Qty", "Unit cost", "GST", "Amount"]]
    for l in po.lines:
        rows.append([Paragraph(_txt(l.description), small), _txt(l.supplier_sku or ""), _qty(l.quantity),
                     _m(l.unit_cost), f"{l.tax_rate:g}%", _m(l.line_total)])
    lines = Table(rows, colWidths=[64 * mm, 26 * mm, 16 * mm, 24 * mm, 14 * mm, 30 * mm], repeatRows=1)
    lines.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9), ("FONTNAME", (0, 0), (-1, -1), font), ("FONTNAME", (0, 0), (-1, 0), bold),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")), ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    totals = Table([["Subtotal (ex GST)", _m(po.subtotal)], ["GST", _m(po.gst)], ["Total", _m(po.total)]],
                   colWidths=[40 * mm, 30 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                                ("FONTNAME", (0, 0), (-1, -1), font), ("FONTNAME", (0, 2), (-1, 2), bold),
                                ("LINEABOVE", (0, 2), (-1, 2), 0.5, colors.black)]))
    story += [lines, Spacer(1, 4 * mm), totals]
    if po.notes:
        story += [Spacer(1, 6 * mm), Paragraph(f"<b>Notes</b><br/>{_txt(po.notes)}", small)]
    story += [Spacer(1, 8 * mm), Paragraph(f"Please quote <b>{_txt(po.number)}</b> on your delivery docket and invoice.", small)]
    doc.build(story)
    return buf.getvalue()
