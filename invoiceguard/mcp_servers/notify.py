"""MCP server: email and Microsoft Teams notifications.

Rules that keep it safe:
- Recipients are keys ("role:ap_manager", "vendor:V1006", "teams:ap-approvals"), never free-text
  addresses. The address comes from the directory, so text in an invoice cannot redirect a message.
- Only code calls these tools (they are not on the model's allow-list).
- NOTIFY_MODE=outbox (default): nothing leaves the app; every message is stored in the outbox.
  NOTIFY_MODE=live: email through Microsoft Graph, Amazon SES or SMTP; Teams through a Workflows webhook.
- Every message, sent or not, is recorded with its status. Rate-limited per recipient.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .common import READ, data_dir, rows, serve

SEND = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
COMPANY_DOMAIN = os.getenv("NOTIFY_COMPANY_DOMAIN", "meridian.example")  # .example never delivers
# Internal people and Teams channels. In a company this comes from Entra ID / the HR system.
INTERNAL = [
    ("role:ap_clerk", "Ana Costa (AP clerk)", f"ana.costa@{COMPANY_DOMAIN}"),
    ("role:ap_manager", "Rui Matos (AP manager)", f"rui.matos@{COMPANY_DOMAIN}"),
    ("role:cfo", "Sofia Reis (CFO)", f"sofia.reis@{COMPANY_DOMAIN}"),
    ("teams:ap-approvals", "Teams: AP approvals", "TEAMS_WEBHOOK_AP_APPROVALS"),
    ("teams:ap-alerts", "Teams: AP fraud alerts", "TEAMS_WEBHOOK_AP_ALERTS"),
]
MAX_PER_HOUR = int(os.getenv("NOTIFY_MAX_PER_HOUR", "30"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS recipients (key TEXT PRIMARY KEY, name TEXT, address TEXT);
CREATE TABLE IF NOT EXISTS outbox (msg_id TEXT PRIMARY KEY, created_at TEXT, channel TEXT, recipient TEXT, name TEXT,
    address TEXT, subject TEXT, body TEXT, ref TEXT, sent_by TEXT, mode TEXT, status TEXT, detail TEXT);
"""

server = MCPServer("invoiceguard-notify", instructions="Send email and Teams notifications to known recipients only.")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(data_dir() / "notify.db", timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    if not con.execute("SELECT 1 FROM recipients LIMIT 1").fetchone():
        con.executemany("INSERT OR IGNORE INTO recipients VALUES (?,?,?)", INTERNAL)
        # Supplier AP contacts. In a company: the contact field of the vendor master.
        erp = sqlite3.connect(data_dir() / "erp.db")
        vendors = erp.execute("SELECT vendor_id, name FROM vendors").fetchall()
        erp.close()
        con.executemany("INSERT OR IGNORE INTO recipients VALUES (?,?,?)",
                        [(f"vendor:{v}", n, f"accounts@{_slug(n)}.example") for v, n in vendors])
        con.commit()
    return con


def _mode() -> str:
    return "live" if os.getenv("NOTIFY_MODE", "outbox") == "live" else "outbox"


# ---------- delivery backends (live mode only) ----------
def _post_json(url: str, payload: dict, headers: dict | None = None) -> None:
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - URLs come from settings, not from input
        if r.status >= 300:
            raise RuntimeError(f"HTTP {r.status}")


def _graph_token() -> str:
    tenant = os.environ["GRAPH_TENANT_ID"]
    body = urllib.parse.urlencode({
        "client_id": os.environ["GRAPH_CLIENT_ID"], "client_secret": os.environ["GRAPH_CLIENT_SECRET"],
        "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials",
    }).encode()
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    with urllib.request.urlopen(urllib.request.Request(url, body), timeout=15) as r:  # noqa: S310
        return json.loads(r.read())["access_token"]


def _send_email(address: str, subject: str, body: str) -> str:
    backend = os.getenv("EMAIL_BACKEND", "graph")
    if backend == "graph":  # Microsoft 365: app registration with Mail.Send, limited to one mailbox
        sender = os.environ["GRAPH_SENDER"]
        _post_json(f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail", {
            "message": {"subject": subject, "body": {"contentType": "Text", "content": body},
                        "toRecipients": [{"emailAddress": {"address": address}}]},
            "saveToSentItems": True,
        }, {"Authorization": f"Bearer {_graph_token()}"})
    elif backend == "ses":  # Amazon SES with the task's IAM role
        import boto3

        boto3.client("sesv2").send_email(
            FromEmailAddress=os.environ["EMAIL_FROM"], Destination={"ToAddresses": [address]},
            Content={"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}}})
    elif backend == "smtp":
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = os.environ["EMAIL_FROM"], address, subject
        msg.set_content(body)
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
            s.starttls()
            if os.getenv("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
    else:
        raise RuntimeError(f"unknown EMAIL_BACKEND {backend}")
    return f"sent via {backend}"


def _post_teams(env_name: str, title: str, text: str, link: str | None) -> str:
    url = os.getenv(env_name)
    if not url:
        raise RuntimeError(f"{env_name} is not set")
    card = {"type": "AdaptiveCard", "version": "1.4", "body": [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": text, "wrap": True}],
        "actions": [{"type": "Action.OpenUrl", "title": "Open in InvoiceGuard", "url": link}] if link else []}
    _post_json(url, {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]})
    return "posted to Teams"


# ---------- tools ----------
def _record(con, channel, rcp, subject, body, ref, sent_by, deliver) -> dict:
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    n = con.execute("SELECT COUNT(*) FROM outbox WHERE recipient=? AND created_at>=?", (rcp["key"], since)).fetchone()[0]
    mode = _mode()
    if n >= MAX_PER_HOUR:
        status, detail = "blocked", f"rate limit: {MAX_PER_HOUR} per hour per recipient"
    elif mode == "outbox":
        status, detail = "simulated", "NOTIFY_MODE=outbox: stored only, nothing left the app"
    else:
        try:
            status, detail = "sent", deliver()
        except Exception as exc:  # never leak secrets: type and a short message only
            status, detail = "failed", f"{type(exc).__name__}: {str(exc)[:160]}"
    msg_id = "MSG-" + uuid.uuid4().hex[:8].upper()
    con.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg_id, datetime.now(UTC).isoformat(timespec="seconds"), channel, rcp["key"], rcp["name"],
                 rcp["address"] if channel == "email" else rcp["key"], subject, body, ref, sent_by, mode, status, detail))
    con.commit()
    return {"msg_id": msg_id, "status": status, "detail": detail, "to": rcp["name"]}


def _recipient(con, key: str, prefix: tuple[str, ...]) -> dict:
    if not key.startswith(prefix):
        raise ToolError(f"recipient must start with {' or '.join(prefix)}")
    r = con.execute("SELECT * FROM recipients WHERE key=?", (key,)).fetchone()
    if not r:
        raise ToolError(f"unknown recipient {key}: only people and suppliers in the directory can be messaged")
    return dict(r)


@server.tool(annotations=SEND)
def send_email(recipient: str, subject: str, body: str, ref: str | None = None, sent_by: str = "system") -> dict:
    """Email a known recipient ("role:ap_manager" or "vendor:V1006"). The address comes from the directory."""
    if len(subject) > 200 or len(body) > 6000:
        raise ToolError("message too long")
    con = _db()
    try:
        rcp = _recipient(con, recipient, ("role:", "vendor:"))
        return _record(con, "email", rcp, subject.replace("\n", " "), body, ref, sent_by,
                       lambda: _send_email(rcp["address"], subject.replace("\n", " "), body))
    finally:
        con.close()


@server.tool(annotations=SEND)
def post_teams(channel: str, title: str, text: str, ref: str | None = None, link: str | None = None,
               sent_by: str = "system") -> dict:
    """Post a card to a known Teams channel ("teams:ap-approvals" or "teams:ap-alerts")."""
    if len(title) > 200 or len(text) > 3000:
        raise ToolError("message too long")
    app_url = os.getenv("APP_URL", "")
    if link and not (app_url and link.startswith(app_url)):
        link = None  # only links back to this app
    con = _db()
    try:
        rcp = _recipient(con, channel, ("teams:",))
        return _record(con, "teams", rcp, title, text, ref, sent_by, lambda: _post_teams(rcp["address"], title, text, link))
    finally:
        con.close()


@server.tool(annotations=READ)
def list_recipients() -> dict:
    """People, suppliers and Teams channels that can receive messages (addresses are not shown)."""
    con = _db()
    try:
        return {"recipients": rows(con.execute("SELECT key, name FROM recipients ORDER BY key"))}
    finally:
        con.close()


@server.tool(annotations=READ)
def list_outbox(ref: str | None = None, limit: int = 100) -> dict:
    """Messages sent or simulated, newest first. Optionally only those for one case."""
    con = _db()
    try:
        q = "SELECT * FROM outbox" + (" WHERE ref=?" if ref else "") + " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        return {"mode": _mode(), "messages": rows(con.execute(q, ((ref,) if ref else ()) + (min(limit, 500),)))}
    finally:
        con.close()


if __name__ == "__main__":
    serve(server)
