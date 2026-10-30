"""A scripted stand-in for Claude, for the free demo and for CI. Not AI.

It answers the same three requests the real model gets (extract, investigate, write a note) with
rules, through the same message format and tool calls, so every other part of the system (MCP
tools, checks, approvals, audit log, evals) runs exactly as it does with Claude.
"""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace

from ..config import APPROVAL_LIMIT_EUR

FIELD = {
    "vendor_vat_id": r"(?:VAT ID|Supplier VAT No\.):\s*(\S+)",
    "invoice_number": r"(?:Invoice number|Invoice No\.):\s*(\S+)",
    "invoice_date": r"(?:Invoice date|Date of issue):\s*(\d{4}-\d{2}-\d{2})",
    "po_number": r"(?:Purchase order|Your order ref\.):\s*(\S+)",
    "iban": r"(?:Pay to IBAN|Bank account \(IBAN\)):\s*([A-Z]{2}[\d ]+)",
}
MONEY = r"(?:EUR\s*)?([\d,]+\.\d{2})(?:\s*EUR)?"


def _num(s: str) -> float:
    return float(s.replace(",", "").replace("EUR", "").strip())


def parse_invoice(text: str) -> dict:
    """Read the two known layouts line by line (this is where a real model earns its keep)."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out: dict = {k: (m.group(1).strip() if (m := re.search(rx, text)) else None) for k, rx in FIELD.items()}
    out["vendor_name"] = lines[1] if len(lines) > 1 else ""
    head = lines.index("Amount") if "Amount" in lines else -1
    stop = next(i for i, x in enumerate(lines) if re.match(r"(Net amount|Subtotal):", x))
    cells = lines[head + 1 : stop]
    width = 5 if "Item" in lines[:head] else 4
    items = []
    for i in range(0, len(cells) - width + 1, width):
        row = cells[i : i + width]
        sku = row[0] if width == 5 else None
        desc, qty, price = row[-4], row[-3], row[-2]
        items.append({"sku": sku, "description": desc, "qty": float(qty), "unit_price": _num(price)})
    out["lines"] = items
    out["net"] = _num(re.search(r"(?:Net amount|Subtotal):\s*" + MONEY, text).group(1))
    vat = re.search(r"(?:VAT|Tax) (\d+)%[^:]*:\s*" + MONEY, text)
    out["vat_rate"], out["vat"] = int(vat.group(1)) / 100, _num(vat.group(2))
    out["gross"] = _num(re.search(r"(?:Total due|Amount payable):\s*" + MONEY, text).group(1))
    out["currency"] = "EUR"
    return out


def _reply(name: str, args: dict, text: str = ""):
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:12], "name": name, "input": args}
    ]
    size = len(json.dumps(args)) // 4
    return SimpleNamespace(content=content, stop_reason="tool_use", usage=SimpleNamespace(input_tokens=800, output_tokens=size))


class _Messages:
    def create(self, **kw):
        names = {t["name"] for t in kw.get("tools", [])}
        msgs = kw["messages"]
        if "record_invoice" in names:
            raw = msgs[0]["content"]
            body = raw.split(">", 1)[1].rsplit("</untrusted_document>", 1)[0]
            return _reply("record_invoice", parse_invoice(body))
        if "submit_findings" in names:
            return self._investigate(msgs, kw.get("tool_choice"))
        if "write_note" in names:
            return _reply("write_note", _note(json.loads(msgs[0]["content"])))
        raise ValueError("scripted model got an unknown request")

    def _investigate(self, msgs: list, tool_choice) -> SimpleNamespace:
        inv = json.loads(msgs[0]["content"].split("\n", 1)[1])
        results: dict[str, dict] = {}
        for m in msgs:
            if m["role"] == "assistant":
                for b in m["content"]:
                    if b["type"] == "tool_use":
                        results[b["id"]] = {"name": b["name"]}
            elif isinstance(m["content"], list):
                for b in m["content"]:
                    if b.get("type") == "tool_result" and b["tool_use_id"] in results:
                        data = json.loads(b["content"]) if not b.get("is_error") else {"error": b["content"]}
                        results[b["tool_use_id"]]["data"] = data
        done = {r["name"]: r.get("data", {}) for r in results.values()}
        vendor = (done.get("find_vendor", {}).get("matches") or [None])[0]
        forced = (tool_choice or {}).get("name") == "submit_findings"
        if "find_vendor" not in done and not forced:
            return _reply("find_vendor", {"query": inv.get("vendor_vat_id") or inv["vendor_name"]},
                          "First I look up the supplier by VAT ID.")
        if vendor and not forced:
            vid = vendor["vendor_id"]
            if "check_bank_account" not in done and inv.get("iban"):
                return _reply("check_bank_account", {"vendor_id": vid, "iban": inv["iban"]})
            if "find_invoices" not in done:
                return _reply("find_invoices", {"vendor_id": vid, "vendor_invoice_no": inv["invoice_number"]})
            if inv.get("po_number") and "get_purchase_order" not in done:
                return _reply("get_purchase_order", {"po_id": inv["po_number"]})
            if inv.get("po_number") and "get_goods_receipts" not in done:
                return _reply("get_goods_receipts", {"po_id": inv["po_number"]})
        reasons, concerns = [], []
        if not vendor:
            concerns.append("Supplier not found in the vendor master.")
        else:
            reasons.append(f"Supplier {vendor['vendor_id']} found, status {vendor['status']}.")
            if vendor["status"] != "active":
                concerns.append("Supplier is not active.")
            if done.get("check_bank_account", {}).get("match") is False:
                concerns.append("IBAN does not match the vendor master.")
            if done.get("find_invoices", {}).get("invoices"):
                concerns.append("An invoice with this number was already booked.")
            if not inv.get("po_number"):
                concerns.append("No purchase order number on the invoice.")
        if any("already booked" in c for c in concerns):
            decision = "reject"
        elif concerns:
            decision = "hold"
        else:
            decision = "approve" if inv["gross"] > APPROVAL_LIMIT_EUR else "pay"
        findings = {"vendor_id": vendor["vendor_id"] if vendor else None, "proposed_decision": decision,
                    "reasons": reasons, "concerns": concerns}
        return _reply("submit_findings", findings)


def _note(facts: dict) -> dict:
    fails = [c for c in facts["checks"] if not c["ok"]]
    d = facts["decision"]
    head = {
        "pay": "All checks passed; payment is scheduled on the due date.",
        "approve": "All checks passed, but the total is above the approval limit, so a person with payment "
        "authority must approve it.",
        "hold": "The invoice is on hold and will not be paid until these points are solved:",
        "reject": "The invoice is rejected:",
    }[d]
    summary = head + ("" if not fails else " " + " ".join(c["detail"] for c in fails[:3]))
    email = None
    if d == "hold" and any(c["code"] in facts["vendor_action_codes"] for c in fails):
        points = "\n".join(f"- {c['detail']}" for c in fails if c["code"] in facts["vendor_action_codes"])
        email = (
            f"Dear {facts['vendor_name']},\n\nThank you for invoice {facts['invoice_number']}. Before we can process "
            f"it, we need the following clarified:\n{points}\n\nPlease send a corrected invoice or the missing "
            "information to this address.\n\nKind regards,\nAccounts Payable, Meridian Transport Group"
        )
    return {"summary": summary, "email_subject": f"Invoice {facts['invoice_number']}: clarification needed" if email else None,
            "email_body": email}


class ScriptedAnthropic:
    """Drop-in for anthropic.Anthropic() in this app: same messages.create() shape."""

    def __init__(self):
        self.messages = _Messages()
