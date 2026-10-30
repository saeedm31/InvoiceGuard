"""MCP server: the ERP (vendors, purchase orders, goods receipts, invoices, payments).

Read tools are marked read-only. Write tools change ERP records. `schedule_payment` moves money,
so it checks a signed approval token and the vendor master again itself (defence in depth):
even a client that calls it directly cannot pay without a valid, unused token.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..config import approval_secret
from ..security import verify_token
from .common import MONEY, READ, WRITE, data_dir, db, mask_iban, rows, serve

server = MCPServer(
    "invoiceguard-erp",
    instructions="Company ERP for accounts payable. Read tools are safe. Write tools are for the workflow code only.",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _vendor_public(v: dict) -> dict:
    v = dict(v)
    v["iban"] = mask_iban(v.get("iban"))  # data minimisation: the full IBAN never leaves the ERP
    return v


# ---------- read ----------
@server.tool(annotations=READ)
def find_vendor(query: str) -> dict:
    """Find suppliers by exact VAT ID or by part of the name. Returns up to 5 matches."""
    q = query.strip()
    with db() as con:
        hits = rows(con.execute("SELECT * FROM vendors WHERE replace(vat_id,' ','') = ?", (q.replace(" ", "").upper(),)))
        if not hits:
            hits = rows(con.execute("SELECT * FROM vendors WHERE name LIKE ? LIMIT 5", (f"%{q}%",)))
    return {"matches": [_vendor_public(v) for v in hits]}


@server.tool(annotations=READ)
def get_vendor(vendor_id: str) -> dict:
    """Vendor master record: name, country, VAT ID, status, payment terms (IBAN masked)."""
    with db() as con:
        r = con.execute("SELECT * FROM vendors WHERE vendor_id=?", (vendor_id,)).fetchone()
    if not r:
        raise ToolError(f"no vendor {vendor_id}")
    return _vendor_public(dict(r))


@server.tool(annotations=READ)
def check_bank_account(vendor_id: str, iban: str) -> dict:
    """Does this IBAN match the vendor master? Answers yes/no without revealing the stored IBAN."""
    with db() as con:
        r = con.execute("SELECT iban FROM vendors WHERE vendor_id=?", (vendor_id,)).fetchone()
    if not r:
        raise ToolError(f"no vendor {vendor_id}")
    return {"vendor_id": vendor_id, "match": r["iban"].replace(" ", "") == iban.replace(" ", "").upper()}


@server.tool(annotations=READ)
def get_purchase_order(po_id: str) -> dict:
    """Purchase order header and lines (item, quantity ordered, agreed unit price)."""
    with db() as con:
        h = con.execute("SELECT * FROM purchase_orders WHERE po_id=?", (po_id.strip(),)).fetchone()
        if not h:
            raise ToolError(f"no purchase order {po_id}")
        lines = rows(con.execute("SELECT line_no, sku, description, qty, unit_price FROM po_lines WHERE po_id=? ORDER BY line_no", (h["po_id"],)))
    return {**dict(h), "lines": lines}


@server.tool(annotations=READ)
def get_goods_receipts(po_id: str) -> dict:
    """Quantities received in the warehouse per purchase-order line."""
    with db() as con:
        rs = rows(con.execute(
            "SELECT line_no, SUM(qty_received) AS qty_received, MAX(received_date) AS last_received "
            "FROM goods_receipts WHERE po_id=? GROUP BY line_no ORDER BY line_no", (po_id.strip(),)))
    return {"po_id": po_id, "receipts": rs}


@server.tool(annotations=READ)
def find_invoices(vendor_id: str | None = None, vendor_invoice_no: str | None = None, po_id: str | None = None) -> dict:
    """Invoices already in the ERP, by supplier, supplier invoice number and/or purchase order."""
    where, args = [], []
    for col, val in (("vendor_id", vendor_id), ("vendor_invoice_no", vendor_invoice_no), ("po_id", po_id)):
        if val:
            where.append(f"{col}=?")
            args.append(val.strip())
    if not where:
        raise ToolError("give at least one filter")
    with db() as con:
        inv = rows(con.execute(
            "SELECT invoice_id, vendor_id, vendor_invoice_no, po_id, invoice_date, net, vat, gross, status FROM invoices "
            f"WHERE {' AND '.join(where)} AND status != 'rejected' ORDER BY invoice_date DESC LIMIT 20", args))
        for i in inv:
            i["lines"] = rows(con.execute("SELECT sku, description, qty, unit_price FROM invoice_lines WHERE invoice_id=?", (i["invoice_id"],)))
    return {"invoices": inv}


# ---------- write (called by workflow code, never offered to the model) ----------
@server.tool(annotations=WRITE)
def post_invoice(
    vendor_invoice_no: str,
    invoice_date: str,
    net: float,
    vat: float,
    gross: float,
    lines: list[dict],
    source_doc: str,
    vendor_id: str | None = None,
    po_id: str | None = None,
    iban: str | None = None,
) -> dict:
    """Book the invoice in the ERP (status 'posted', or 'parked' without a known vendor). Not a payment."""
    invoice_id = "INV-" + uuid.uuid4().hex[:10].upper()
    status = "posted" if vendor_id else "parked"
    with db() as con:
        con.execute(
            "INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (invoice_id, vendor_id, vendor_invoice_no, po_id, invoice_date, net, vat, gross, iban, status, source_doc, _now(), None),
        )
        for ln in lines:
            con.execute("INSERT INTO invoice_lines VALUES (?,?,?,?,?)",
                        (invoice_id, ln.get("sku"), ln["description"], ln["qty"], ln["unit_price"]))
    return {"invoice_id": invoice_id, "status": status}


def _set_status(invoice_id: str, status: str, note: str) -> dict:
    with db() as con:
        cur = con.execute("UPDATE invoices SET status=?, note=? WHERE invoice_id=?", (status, note[:1000], invoice_id))
        if not cur.rowcount:
            raise ToolError(f"no invoice {invoice_id}")
    return {"invoice_id": invoice_id, "status": status}


@server.tool(annotations=WRITE)
def hold_invoice(invoice_id: str, reasons: list[str]) -> dict:
    """Block an invoice from payment until the listed problems are solved."""
    return _set_status(invoice_id, "on_hold", "; ".join(reasons))


@server.tool(annotations=WRITE)
def reject_invoice(invoice_id: str, reason: str) -> dict:
    """Reject an invoice (e.g. a duplicate). It will never be paid."""
    return _set_status(invoice_id, "rejected", reason)


@server.tool(annotations=WRITE)
def request_approval(invoice_id: str, reason: str) -> dict:
    """Mark an invoice as waiting for a person with payment authority."""
    return _set_status(invoice_id, "awaiting_approval", reason)


@server.tool(annotations=MONEY)
def schedule_payment(invoice_id: str, approval_token: str) -> dict:
    """Schedule payment of a posted invoice. Needs a valid, unused approval token for this invoice and amount."""
    with db() as con:
        inv = con.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        if not inv:
            raise ToolError(f"no invoice {invoice_id}")
        if inv["status"] not in ("posted", "awaiting_approval"):
            raise ToolError(f"invoice is {inv['status']}, cannot pay")
        ok, reason, claims = verify_token(approval_secret(data_dir()), approval_token, invoice_id, inv["gross"])
        if not ok:
            raise ToolError(f"payment refused: {reason}")
        v = con.execute("SELECT * FROM vendors WHERE vendor_id=?", (inv["vendor_id"],)).fetchone()
        if not v or v["status"] != "active":
            raise ToolError("payment refused: vendor not active")
        if (inv["iban"] or "").replace(" ", "") != v["iban"].replace(" ", ""):
            raise ToolError("payment refused: IBAN differs from vendor master")
        if con.execute("SELECT 1 FROM payments WHERE token_id=?", (claims["jti"],)).fetchone():
            raise ToolError("payment refused: token already used")
        due = date.fromisoformat(inv["invoice_date"]) + timedelta(days=v["payment_terms_days"])
        pay_id = "PAY-" + uuid.uuid4().hex[:8].upper()
        con.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?)",
                    (pay_id, invoice_id, inv["gross"], due.isoformat(), claims["by"], claims["jti"], _now()))
        con.execute("UPDATE invoices SET status='scheduled' WHERE invoice_id=?", (invoice_id,))
    return {"payment_id": pay_id, "invoice_id": invoice_id, "amount": inv["gross"], "pay_on": due.isoformat(), "approved_by": claims["by"]}


if __name__ == "__main__":
    serve(server)
