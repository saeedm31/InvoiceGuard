"""Render simulated invoices as PDFs, in two supplier layouts with different labels."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

LABELS = {
    "A": dict(title="INVOICE", vat="VAT ID", number="Invoice number", date="Invoice date", due="Due date",
              po="Purchase order", net="Net amount", tax="VAT", total="Total due", iban="Pay to IBAN"),
    "B": dict(title="TAX INVOICE", vat="Supplier VAT No.", number="Invoice No.", date="Date of issue",
              due="Payment due", po="Your order ref.", net="Subtotal", tax="Tax", total="Amount payable",
              iban="Bank account (IBAN)"),
}


def money(x: float, layout: str) -> str:
    return f"EUR {x:,.2f}" if layout == "A" else f"{x:.2f} EUR"


def render_invoice(path: Path, case, buyer: dict) -> None:
    lab = LABELS[case.layout]
    v = case.vendor
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"{lab['title']} {case.number}")
    w, h = A4
    y = h - 60
    c.setFillColor(HexColor("#16253D"))
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, y, lab["title"])
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(w - 50, y, v.name)
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 50, y - 14, v.address)
    c.drawRightString(w - 50, y - 26, f"{lab['vat']}: {v.vat}")
    y -= 60
    c.setFont("Helvetica", 10)
    due = case.issued + timedelta(days=v.terms_days)
    rows = [(lab["number"], case.number), (lab["date"], case.issued.isoformat()), (lab["due"], due.isoformat())]
    rows.append((lab["po"], case.po_id or "-"))
    for k, val in rows:
        c.drawString(50, y, f"{k}: {val}")
        y -= 14
    y -= 8
    c.drawString(50, y, f"Bill to: {buyer['name']}, {buyer['address']}, VAT {buyer['vat']}")
    y -= 28
    c.setFont("Helvetica-Bold", 10)
    head = ["Item", "Description", "Qty", "Unit price", "Amount"] if case.layout == "B" else ["Description", "Qty", "Unit price", "Amount"]
    xs = [50, 120, 330, 400, 480] if case.layout == "B" else [50, 330, 400, 480]
    for x, t in zip(xs, head, strict=True):
        c.drawString(x, y, t)
    c.setFont("Helvetica", 10)
    y -= 16
    for ln in case.lines:
        amount = round(ln["qty"] * ln["unit_price"], 2)
        cells = [ln["description"], f"{ln['qty']:g}", money(ln["unit_price"], case.layout), money(amount, case.layout)]
        if case.layout == "B":
            cells = [ln["sku"]] + cells
        for x, t in zip(xs, cells, strict=True):
            c.drawString(x, y, t)
        y -= 14
    y -= 14
    tax_label = f"{lab['tax']} {case.vat_rate:.0%}" if case.vat_rate else f"{lab['tax']} 0% (reverse charge, Art. 196 VAT Directive)"
    for k, val in ((lab["net"], case.net), (tax_label, case.vat), (lab["total"], case.gross)):
        c.drawString(50, y, f"{k}: {money(val, case.layout)}")
        y -= 14
    y -= 14
    c.drawString(50, y, f"{lab['iban']}: {case.iban}")
    y -= 14
    c.drawString(50, y, f"Payment terms: {v.terms_days} days net")
    if case.note:
        y -= 28
        c.setFont("Helvetica-Bold", 10)
        c.drawString(50, y, case.note[:95])
        if len(case.note) > 95:
            c.drawString(50, y - 13, case.note[95:190])
    if case.hidden:  # near-white, tiny: invisible when printed, still in the text layer
        c.setFillColor(Color(0.985, 0.985, 0.985))
        c.setFont("Helvetica", 4)
        c.drawString(50, 40, case.hidden)
    c.showPage()
    c.save()
