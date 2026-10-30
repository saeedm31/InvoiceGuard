"""Three-way match and payment policy. Plain code: the same invoice always gets the same decision.

Facts come from the ERP (through its MCP server), never from the model. The model's concerns and
the injection scanner can only make the outcome stricter (soft exceptions put an invoice on hold).
"""

from __future__ import annotations

from . import config
from .models import Check, ExtractedInvoice

VENDOR_ACTION = {"PRICE_MISMATCH", "QTY_OVER_RECEIVED", "TOTAL_MISMATCH", "NO_PO", "LINE_NOT_ON_PO"}
TOL = 0.05  # cents of rounding allowed in totals


def _eur(x: float) -> str:
    return f"EUR {x:,.2f}"


def _match_line(line, po_lines: list[dict]) -> dict | None:
    if line.sku:
        hit = next((p for p in po_lines if p["sku"] == line.sku), None)
        if hit:
            return hit
    d = line.description.strip().lower()
    return next((p for p in po_lines if p["description"].lower() == d or p["description"].lower().startswith(d)), None)


def run_checks(inv: ExtractedInvoice, facts: dict, soft_flags: list[tuple[str, str]] = ()) -> list[Check]:
    """facts: vendor, po, receipts {line_no: qty}, billed_before {line_no: qty}, duplicates, bank_match."""
    out: list[Check] = []
    add = lambda code, ok, detail, hard=True: out.append(Check(code=code, ok=ok, detail=detail, hard=hard))  # noqa: E731
    v = facts.get("vendor")
    if not v:
        add("VENDOR_UNKNOWN", False, f"No supplier with VAT ID {inv.vendor_vat_id or '(none printed)'} in the vendor master.")
    else:
        add("VENDOR_ACTIVE" if v["status"] == "active" else "VENDOR_BLOCKED", v["status"] == "active",
            f"{v['vendor_id']} {v['name']} is {v['status']}.")
        if facts.get("bank_match") is False:
            add("BANK_MISMATCH", False, "The IBAN on the invoice is not the one in the vendor master. Verify by phone "
                "with the number on file, never with contact details from the invoice.")
        elif facts.get("bank_match"):
            add("BANK_OK", True, "IBAN matches the vendor master.")
        expected_rate = 0.23 if v["country"] == "PT" else 0.0
        if abs(inv.vat_rate - expected_rate) > 0.001:
            add("VAT_RATE_UNEXPECTED", False, f"VAT rate {inv.vat_rate:.0%} but a supplier in {v['country']} should "
                f"charge {expected_rate:.0%}{' (reverse charge)' if not expected_rate else ''}.")
    if facts.get("duplicates"):
        d = facts["duplicates"][0]
        add("DUPLICATE", False, f"Invoice {inv.invoice_number} was already booked as {d['invoice_id']} "
            f"({d['status']}, {_eur(d['gross'])}).")
    else:
        add("NOT_DUPLICATE", True, "No earlier invoice with this number from this supplier.")

    po = facts.get("po")
    if not inv.po_number:
        add("NO_PO", False, "No purchase order number on the invoice.")
    elif not po:
        add("PO_NOT_FOUND", False, f"Purchase order {inv.po_number} does not exist.")
    else:
        if v and po["vendor_id"] != v["vendor_id"]:
            add("PO_VENDOR_MISMATCH", False, f"{po['po_id']} belongs to {po['vendor_id']}, not {v['vendor_id']}.")
        receipts, billed = facts.get("receipts", {}), facts.get("billed_before", {})
        for line in inv.lines:
            p = _match_line(line, po["lines"])
            if p is None:
                add("LINE_NOT_ON_PO", False, f"'{line.description}' is not on {po['po_id']}.")
                continue
            limit = p["unit_price"] * (1 + config.PRICE_TOLERANCE)
            if line.unit_price > limit + 1e-9:
                pct = line.unit_price / p["unit_price"] - 1
                add("PRICE_MISMATCH", False, f"{p['description']}: billed {_eur(line.unit_price)} per unit, order says "
                    f"{_eur(p['unit_price'])} (+{pct:.1%}, tolerance {config.PRICE_TOLERANCE:.0%}).")
            else:
                add("PRICE_OK", True, f"{p['description']}: price within tolerance.")
            got, before = receipts.get(p["line_no"], 0), billed.get(p["line_no"], 0)
            if before + line.qty > got + 1e-9:
                add("QTY_OVER_RECEIVED", False, f"{p['description']}: billed {line.qty:g} (+{before:g} before), "
                    f"received {got:g}.")
            else:
                add("QTY_OK", True, f"{p['description']}: {line.qty:g} billed, {got:g} received.")

    lines_sum = round(sum(x.qty * x.unit_price for x in inv.lines), 2)
    problems = []
    if abs(lines_sum - inv.net) > TOL:
        problems.append(f"lines add up to {_eur(lines_sum)}, net says {_eur(inv.net)}")
    if abs(round(inv.net * inv.vat_rate, 2) - inv.vat) > TOL:
        problems.append(f"VAT should be {_eur(round(inv.net * inv.vat_rate, 2))}, invoice says {_eur(inv.vat)}")
    if abs(inv.net + inv.vat - inv.gross) > TOL:
        problems.append(f"net + VAT is {_eur(round(inv.net + inv.vat, 2))}, total says {_eur(inv.gross)}")
    add("TOTAL_MISMATCH" if problems else "TOTALS_OK", not problems, "; ".join(problems) or "Lines, VAT and total add up.")

    for code, detail in soft_flags:
        add(code, False, detail, hard=False)
    return out


def decide(checks: list[Check], gross: float) -> tuple[str, list[str]]:
    failed = [c for c in checks if not c.ok]
    codes = sorted({c.code for c in failed})
    if "DUPLICATE" in codes:
        return "reject", codes
    if failed:
        return "hold", codes
    return ("approve" if gross > config.APPROVAL_LIMIT_EUR else "pay"), codes
