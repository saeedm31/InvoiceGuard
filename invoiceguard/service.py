"""Cases and human approvals.

A case waiting for approval is a row in the database, not a paused Python process: it survives
restarts and redeploys, and approval can come days later from any app instance. Approving mints a
signed token for exactly that invoice and amount; the ERP's payment tool checks it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .agent.graph import InvoiceAgent
from .agent.scripted import ScriptedAnthropic
from .audit import AuditLog
from .config import TOKEN_TTL_S, Settings, approval_secret
from .llm import Meter, MeteredClient, make_client
from .mcp_hub import MCPHub
from .security import mint_token

# Who may approve what (EUR). In a company this comes from the HR system / SSO groups.
APPROVERS = {
    "ana.costa": ("AP clerk", 0.0),
    "rui.matos": ("AP manager", 50_000.0),
    "sofia.reis": ("CFO", float("inf")),
}
SCHEMA = """CREATE TABLE IF NOT EXISTS cases (case_id TEXT PRIMARY KEY, doc_id TEXT, created TEXT, status TEXT,
    decision TEXT, exceptions TEXT, gross REAL, vendor TEXT, invoice_id TEXT, mode TEXT, cost_eur REAL, seconds REAL,
    approved_by TEXT, payload TEXT)"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Service:
    def __init__(self, settings: Settings, hub: MCPHub):
        self.s, self.hub = settings, hub
        self.audit = AuditLog(settings.audit_db)
        with self._con() as con:
            con.execute(SCHEMA)

    def _con(self):
        con = sqlite3.connect(self.s.cases_db, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def agent(self, mode: str = "scripted", api_key: str | None = None, model: str | None = None, meter: Meter | None = None):
        if mode == "scripted":
            llm = MeteredClient(ScriptedAnthropic(), meter)
            return InvoiceAgent(llm, self.hub, self.audit, "scripted", "scripted")
        llm = make_client(self.s.llm_provider, api_key=api_key, region=self.s.aws_region, meter=meter)
        return InvoiceAgent(llm, self.hub, self.audit, model or self.s.agent_model, self.s.writer_model)

    def process(self, doc_id: str, mode: str = "scripted", **kw) -> dict:
        res = self.agent(mode, **kw).run(doc_id)
        inv = res.get("extracted") or {}
        vendor = ((res.get("findings") or {}).get("vendor_id")) or inv.get("vendor_name")
        with self._con() as con:
            con.execute(
                "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (res["case_id"], doc_id, _now(), res["status"], res["decision"], json.dumps(res["exceptions"]),
                 inv.get("gross"), vendor, res.get("invoice_id"), mode, res["cost"]["eur"], res["seconds"], None,
                 json.dumps(res, default=str)),
            )
        return res

    def reset(self) -> None:
        """Rebuild the simulated company: fresh ERP, inbox, no cases, new audit log."""
        from .simulate import build

        build(self.s.data_dir)
        self.audit = AuditLog(self.s.audit_db)
        with self._con() as con:
            con.execute(SCHEMA)

    def cases(self) -> list[dict]:
        with self._con() as con:
            return [dict(r) for r in con.execute("SELECT * FROM cases ORDER BY created DESC")]

    def case(self, case_id: str) -> dict | None:
        with self._con() as con:
            r = con.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        return d

    def _update(self, case_id: str, status: str, approver: str | None) -> None:
        with self._con() as con:
            con.execute("UPDATE cases SET status=?, approved_by=? WHERE case_id=?", (status, approver, case_id))

    def approve(self, case_id: str, approver: str) -> dict:
        """A person with enough payment authority approves; code mints the token and schedules payment."""
        c = self.case(case_id)
        if not c or c["status"] != "awaiting_approval":
            return {"ok": False, "error": "This case is not waiting for approval."}
        role, limit = APPROVERS.get(approver, ("unknown", 0.0))
        if c["gross"] > limit:
            self.audit.write("approval_refused", dict(approver=approver, role=role, limit=limit, gross=c["gross"]),
                             run_id=case_id, actor=approver)
            return {"ok": False, "error": f"{approver} ({role}) can approve up to EUR {limit:,.0f}; this invoice is "
                                          f"EUR {c['gross']:,.2f}."}
        token = mint_token(approval_secret(self.s.data_dir), c["invoice_id"], c["gross"], f"human:{approver}", TOKEN_TTL_S)
        r = self.hub.call("schedule_payment", {"invoice_id": c["invoice_id"], "approval_token": token})
        self.audit.write("human_approval", dict(approver=approver, role=role, ok=r.ok, payment=r.data, error=r.error),
                         run_id=case_id, actor=approver)
        if r.ok:
            self._update(case_id, "scheduled", approver)
            return {"ok": True, "payment": r.data}
        return {"ok": False, "error": r.error}

    def decline(self, case_id: str, approver: str, reason: str) -> dict:
        c = self.case(case_id)
        if not c or c["status"] != "awaiting_approval":
            return {"ok": False, "error": "This case is not waiting for approval."}
        r = self.hub.call("hold_invoice", {"invoice_id": c["invoice_id"], "reasons": [f"Declined by {approver}: {reason}"]})
        self.audit.write("human_decline", dict(approver=approver, reason=reason, ok=r.ok), run_id=case_id, actor=approver)
        self._update(case_id, "on_hold", approver)
        return {"ok": r.ok}


def open_service(data_dir: Path | None = None) -> Service:
    from .simulate import ensure_built

    s = Settings() if data_dir is None else Settings(data_dir=Path(data_dir))
    ensure_built(s.data_dir)
    return Service(s, MCPHub(s.data_dir))
