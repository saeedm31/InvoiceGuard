"""The invoice workflow as a LangGraph state machine.

    read -> extract (LLM) -> agent (LLM) <-> tools (read-only MCP) -> check (code) -> explain (LLM) -> act (code)

- read:    the documents MCP server returns the PDF text; a scanner flags injection attempts.
- extract: the model fills a fixed schema from the text. It has no other tool, so instructions hidden in the
           document can at worst produce wrong fields, and the checks compare every field with the ERP.
- agent:   a second model call sees only the extracted fields (never the raw document) and investigates with
           read-only ERP tools, then proposes a decision.
- check:   code fetches the same facts itself and applies the payment policy. The model can only make it stricter.
- explain: the model writes the clerk's note and a supplier email; numbers are checked against the facts.
- act:     code books the invoice and pays, holds, rejects or asks a person. Payment needs a signed token.

Nodes are plain methods, so the same steps run without LangGraph (a small runner follows the same edges).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, TypedDict

from pydantic import ValidationError

from .. import checks as rules
from ..audit import AuditLog
from ..config import MAX_AGENT_STEPS, TOKEN_TTL_S, approval_secret
from ..llm import MeteredClient, total
from ..mcp_hub import MCPHub
from ..mcp_servers.common import mask_iban
from ..models import ExtractedInvoice, Findings
from ..security import mint_token, scan, untrusted
from . import prompts
from .grounding import unsupported

try:
    from langgraph.graph import END, StateGraph

    HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover - the plain runner below takes over
    END, HAS_LANGGRAPH = "__end__", False


class State(TypedDict, total=False):
    case_id: str
    doc_id: str
    text: str
    scan: list
    extracted: dict | None
    messages: list
    pending: bool
    steps: int
    tool_calls: list
    findings: dict | None
    facts: dict
    checks: list
    decision: str
    exceptions: list
    note: dict
    actions: list
    invoice_id: str | None
    status: str


def _block(b: Any) -> dict:
    if isinstance(b, dict):
        return b
    if b.type == "text":
        return {"type": "text", "text": b.text}
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {"type": b.type}


class InvoiceAgent:
    def __init__(self, llm: MeteredClient, hub: MCPHub, audit: AuditLog, model: str, writer_model: str, actor: str = "ap-bot"):
        self.llm, self.hub, self.audit = llm, hub, audit
        self.model, self.writer_model, self.actor = model, writer_model, actor
        self.graph = self._compile() if HAS_LANGGRAPH else None

    # ---------- helpers ----------
    def _log(self, state: State, kind: str, payload: dict) -> None:
        self.audit.write(kind, payload, run_id=state.get("case_id"), actor=self.actor)

    def _tool(self, state: State, name: str, args: dict, by: str = "code"):
        r = self.hub.call(name, args)
        logged = {k: ("[token]" if k == "approval_token" else mask_iban(v) if k == "iban" else v) for k, v in args.items()}
        self._log(state, "tool_call", dict(by=by, tool=name, args=logged, ok=r.ok, ms=r.ms, error=r.error,
                                           result=json.dumps(r.data, default=str)[:600] if r.data else None))
        return r

    def _model(self, state: State, step: str, model: str, **kw):
        mark = len(self.llm.meter.calls)
        t0 = time.time()
        resp = self.llm.create(step=step, model=model, max_tokens=kw.pop("max_tokens", 1500), **kw)
        content = [_block(b) for b in resp.content]
        self._log(state, "llm_call", dict(step=step, model=model, stop_reason=getattr(resp, "stop_reason", None),
                                          tools=[b["name"] for b in content if b["type"] == "tool_use"],
                                          usage=[u.to_dict() for u in self.llm.meter.since(mark)],
                                          ms=int(1000 * (time.time() - t0))))
        return content

    # ---------- nodes ----------
    def read(self, state: State) -> State:
        r = self._tool(state, "read_invoice", {"doc_id": state["doc_id"]})
        if not r.ok:
            return {"text": "", "scan": [], "status": "failed"}
        hits = scan(r.data["text"])
        if hits:
            self._log(state, "security_flag", dict(source=state["doc_id"], hits=hits))
        return {"text": r.data["text"], "scan": hits}

    def extract(self, state: State) -> State:
        if not state.get("text"):
            return {"extracted": None}
        messages = [{"role": "user", "content": untrusted(state["text"], state["doc_id"])}]
        for attempt in range(2):
            content = self._model(state, "extract", self.model, system=prompts.EXTRACT, messages=messages,
                                  tools=[prompts.RECORD_INVOICE], tool_choice={"type": "tool", "name": "record_invoice"})
            call = next((b for b in content if b["type"] == "tool_use"), None)
            try:
                inv = ExtractedInvoice.model_validate(call["input"] if call else {})
                self._log(state, "extracted", inv.model_dump())
                return {"extracted": inv.model_dump()}
            except ValidationError as exc:
                self._log(state, "extract_invalid", dict(attempt=attempt, errors=exc.errors(include_url=False)[:5]))
                if call is None:
                    break
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "is_error": True,
                                                  "content": f"Invalid: {exc.errors(include_url=False)[:5]}. Fix and call again."}]},
                ]
        return {"extracted": None}

    def agent(self, state: State) -> State:
        messages = state.get("messages") or [
            {"role": "user", "content": "Invoice fields (extracted):\n" + json.dumps(state["extracted"])}
        ]
        steps = state.get("steps", 0)
        kw: dict = {}
        if steps >= MAX_AGENT_STEPS:
            kw["tool_choice"] = {"type": "tool", "name": "submit_findings"}  # out of budget: report now
        content = self._model(state, "investigate", self.model, system=prompts.INVESTIGATE, messages=messages,
                              tools=self.hub.agent_tool_specs() + [prompts.SUBMIT_FINDINGS], **kw)
        messages = messages + [{"role": "assistant", "content": content}]
        submit = next((b for b in content if b["type"] == "tool_use" and b["name"] == "submit_findings"), None)
        if submit:
            try:
                f = Findings.model_validate(submit["input"]).model_dump()
            except ValidationError:
                f = {"proposed_decision": "hold", "reasons": [], "concerns": ["Model returned invalid findings."], "vendor_id": None}
            self._log(state, "findings", f)
            return {"messages": messages, "pending": False, "findings": f}
        wants = any(b["type"] == "tool_use" for b in content)
        if not wants:  # answered in text without submitting: treat as no proposal
            return {"messages": messages, "pending": False, "findings": None}
        return {"messages": messages, "pending": True}

    def tools(self, state: State) -> State:
        last = state["messages"][-1]["content"]
        results, calls = [], list(state.get("tool_calls", []))
        for b in (x for x in last if x["type"] == "tool_use"):
            if not self.hub.agent_may_call(b["name"]):  # allow-list, checked again at call time
                self._log(state, "tool_blocked", dict(tool=b["name"], args=b.get("input")))
                results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                                "content": f"Tool {b['name']} is not available to you."})
                continue
            r = self._tool(state, b["name"], b.get("input") or {}, by="model")
            calls.append(dict(name=b["name"], input=b.get("input") or {}, ok=r.ok, ms=r.ms))
            body = json.dumps(r.data, default=str)[:12_000] if r.ok else (r.error or "error")
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": body, "is_error": not r.ok})
        return {"messages": state["messages"] + [{"role": "user", "content": results}], "tool_calls": calls,
                "steps": state.get("steps", 0) + 1, "pending": False}

    def check(self, state: State) -> State:
        """Facts straight from the ERP (code calls, not the model's), then the rules."""
        if not state.get("extracted"):
            c = [dict(code="EXTRACTION_FAILED", ok=False, detail="The invoice could not be read reliably.", hard=True)]
            return {"facts": {}, "checks": c, "decision": "hold", "exceptions": ["EXTRACTION_FAILED"]}
        inv = ExtractedInvoice.model_validate(state["extracted"])
        facts: dict = {"vendor": None, "po": None, "receipts": {}, "billed_before": {}, "duplicates": [], "bank_match": None}
        r = self._tool(state, "find_vendor", {"query": inv.vendor_vat_id}) if inv.vendor_vat_id else None
        matches = (r.data or {}).get("matches", []) if r and r.ok else []
        vendor = matches[0] if len(matches) == 1 else None
        if vendor:
            facts["vendor"] = vendor
            if inv.iban:
                b = self._tool(state, "check_bank_account", {"vendor_id": vendor["vendor_id"], "iban": inv.iban})
                facts["bank_match"] = b.data["match"] if b.ok else None
            d = self._tool(state, "find_invoices", {"vendor_id": vendor["vendor_id"], "vendor_invoice_no": inv.invoice_number})
            facts["duplicates"] = d.data["invoices"] if d.ok else []
        if inv.po_number:
            p = self._tool(state, "get_purchase_order", {"po_id": inv.po_number})
            if p.ok:
                facts["po"] = p.data
                g = self._tool(state, "get_goods_receipts", {"po_id": inv.po_number})
                facts["receipts"] = {x["line_no"]: x["qty_received"] for x in (g.data["receipts"] if g.ok else [])}
                prev = self._tool(state, "find_invoices", {"po_id": inv.po_number})
                billed: dict[int, float] = {}
                for old in prev.data["invoices"] if prev.ok else []:
                    for ln in old["lines"]:
                        hit = next((x for x in p.data["lines"] if x["sku"] == ln["sku"] or x["description"] == ln["description"]), None)
                        if hit:
                            billed[hit["line_no"]] = billed.get(hit["line_no"], 0) + ln["qty"]
                facts["billed_before"] = billed
        soft = [("SUSPICIOUS_CONTENT", "The document contains text aimed at automated systems: "
                 + "; ".join(f"{h['rule']} ('{h['snippet'][:80]}')" for h in state.get("scan", [])[:2]))] if state.get("scan") else []
        found = rules.run_checks(inv, facts, soft)
        decision, codes = rules.decide(found, inv.gross)
        f = state.get("findings") or {}
        order = ["pay", "approve", "hold", "reject"]
        agree = f.get("proposed_decision") == decision
        if f and order.index(f.get("proposed_decision", "hold")) > order.index(decision) and decision in ("pay", "approve"):
            # the model is more careful than the rules: a person looks at it
            found.append(rules.Check(code="MODEL_CONCERN", ok=False, hard=False,
                                     detail="The investigating model proposed '" + f["proposed_decision"] + "': "
                                     + "; ".join(f.get("concerns") or f.get("reasons") or [])[:300]))
            decision, codes = rules.decide(found, inv.gross)
        out_checks = [c.model_dump() for c in found]
        self._log(state, "decision", dict(decision=decision, exceptions=codes, model_proposed=f.get("proposed_decision"),
                                          agrees_with_model=agree, gross=inv.gross))
        return {"facts": facts, "checks": out_checks, "decision": decision, "exceptions": codes}

    def explain(self, state: State) -> State:
        inv = state.get("extracted") or {}
        v = (state.get("facts") or {}).get("vendor") or {}
        facts = {
            "decision": state["decision"],
            "invoice_number": inv.get("invoice_number"),
            "vendor_name": v.get("name") or inv.get("vendor_name"),
            "gross": inv.get("gross"),
            "checks": state["checks"],
            "vendor_action_codes": sorted(rules.VENDOR_ACTION),
        }
        content = self._model(state, "explain", self.writer_model, system=prompts.EXPLAIN, max_tokens=800,
                              messages=[{"role": "user", "content": json.dumps(facts, default=str)}],
                              tools=[prompts.WRITE_NOTE], tool_choice={"type": "tool", "name": "write_note"})
        call = next((b for b in content if b["type"] == "tool_use"), None)
        note = dict(call["input"]) if call else {"summary": ""}
        bad = unsupported(" ".join(filter(None, [note.get("summary"), note.get("email_body")])), [facts, inv])
        note["grounded"] = not bad
        if bad:  # never show a number that is not in the facts; fall back to the check details
            self._log(state, "grounding_failed", dict(numbers=bad))
            note["summary"] = " ".join(c["detail"] for c in state["checks"] if not c["ok"]) or "All checks passed."
            note["email_body"] = None
        return {"note": note}

    def act(self, state: State) -> State:
        inv, d = state.get("extracted"), state["decision"]
        acts: list = []
        if not inv:
            return {"actions": acts, "invoice_id": None, "status": "needs_review"}
        v = (state.get("facts") or {}).get("vendor")
        posted = self._tool(state, "post_invoice", dict(
            vendor_id=v["vendor_id"] if v else None, vendor_invoice_no=inv["invoice_number"], po_id=inv.get("po_number"),
            invoice_date=inv["invoice_date"], net=inv["net"], vat=inv["vat"], gross=inv["gross"], iban=inv.get("iban"),
            lines=inv["lines"], source_doc=state["doc_id"]))
        if not posted.ok:
            return {"actions": [("post_invoice", posted.error)], "invoice_id": None, "status": "failed"}
        iid = posted.data["invoice_id"]
        acts.append(("posted", iid))
        reasons = [c["detail"] for c in state["checks"] if not c["ok"]]
        if d == "pay":
            token = mint_token(approval_secret(self.hub.data_dir), iid, inv["gross"], "policy:auto", TOKEN_TTL_S)
            p = self._tool(state, "schedule_payment", {"invoice_id": iid, "approval_token": token})
            status = "scheduled" if p.ok else "failed"
            acts.append(("payment", p.data if p.ok else p.error))
        elif d == "approve":
            self._tool(state, "request_approval", {"invoice_id": iid, "reason": f"Total above the approval limit: {inv['gross']}"})
            status = "awaiting_approval"
        elif d == "reject":
            self._tool(state, "reject_invoice", {"invoice_id": iid, "reason": "; ".join(reasons)})
            status = "rejected"
        else:
            self._tool(state, "hold_invoice", {"invoice_id": iid, "reasons": reasons})
            status = "on_hold"
            note = state.get("note") or {}
            if v and note.get("email_body"):
                m = self._tool(state, "draft_email", {"vendor_id": v["vendor_id"], "subject": note.get("email_subject")
                                                      or f"Invoice {inv['invoice_number']}", "body": note["email_body"]})
                if m.ok:
                    acts.append(("email_draft", m.data["draft_id"]))
        return {"actions": acts, "invoice_id": iid, "status": status}

    # ---------- wiring ----------
    @staticmethod
    def _after_extract(state: State) -> str:
        return "agent" if state.get("extracted") else "check"

    @staticmethod
    def _after_agent(state: State) -> str:
        return "tools" if state.get("pending") else "check"

    def _compile(self):
        g = StateGraph(State)
        for name in ("read", "extract", "agent", "tools", "check", "explain", "act"):
            g.add_node(name, getattr(self, name))
        g.set_entry_point("read")
        g.add_edge("read", "extract")
        g.add_conditional_edges("extract", self._after_extract, {"agent": "agent", "check": "check"})
        g.add_conditional_edges("agent", self._after_agent, {"tools": "tools", "check": "check"})
        g.add_edge("tools", "agent")
        g.add_edge("check", "explain")
        g.add_edge("explain", "act")
        g.add_edge("act", END)
        return g.compile()

    def _run_plain(self, state: State) -> State:
        node = "read"
        nxt = {"read": lambda s: "extract", "extract": self._after_extract, "agent": self._after_agent,
               "tools": lambda s: "agent", "check": lambda s: "explain", "explain": lambda s: "act", "act": lambda s: END}
        for _ in range(4 * MAX_AGENT_STEPS + 12):
            state = {**state, **getattr(self, node)(state)}
            node = nxt[node](state)
            if node == END:
                return state
        raise RuntimeError("workflow did not finish")

    def run(self, doc_id: str) -> dict:
        case_id = "CASE-" + uuid.uuid4().hex[:8].upper()
        t0, mark = time.time(), len(self.llm.meter.calls)
        state: State = {"case_id": case_id, "doc_id": doc_id, "steps": 0, "tool_calls": [], "messages": []}
        self._log(state, "case_start", dict(doc_id=doc_id, model=self.model, engine="langgraph" if self.graph else "plain"))
        if self.graph is not None:
            state = self.graph.invoke(state, config={"recursion_limit": 4 * MAX_AGENT_STEPS + 12, "run_name": "invoiceguard",
                                                     "tags": [case_id]})
        else:
            state = self._run_plain(state)
        cost = total(self.llm.meter.since(mark))
        out = {k: state.get(k) for k in ("case_id", "doc_id", "extracted", "findings", "checks", "decision", "exceptions",
                                         "note", "actions", "invoice_id", "status", "tool_calls", "scan")}
        out.update(cost=cost, seconds=round(time.time() - t0, 2), engine="langgraph" if self.graph else "plain",
                   steps=state.get("steps", 0))
        self._log(state, "case_end", dict(decision=out["decision"], status=out["status"], exceptions=out["exceptions"],
                                          cost_eur=cost["eur"], seconds=out["seconds"]))
        return out
