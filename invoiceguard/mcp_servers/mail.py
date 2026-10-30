"""MCP server: email drafts to suppliers. It can only draft; a person sends."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .common import READ, WRITE, db, rows, serve

server = MCPServer("invoiceguard-mail", instructions="Draft emails to suppliers. There is no send tool on purpose.")


@server.tool(annotations=WRITE)
def draft_email(vendor_id: str, subject: str, body: str) -> dict:
    """Save an email draft to a supplier for a person to review and send."""
    if len(body) > 4000 or len(subject) > 200:
        raise ToolError("draft too long")
    with db() as con:
        if not con.execute("SELECT 1 FROM vendors WHERE vendor_id=?", (vendor_id,)).fetchone():
            raise ToolError(f"unknown vendor {vendor_id}")
        draft_id = "DRF-" + uuid.uuid4().hex[:8].upper()
        con.execute(
            "INSERT INTO email_drafts VALUES (?,?,?,?,?,?)",
            (draft_id, vendor_id, subject, body, datetime.now(UTC).isoformat(timespec="seconds"), "draft"),
        )
    return {"draft_id": draft_id, "status": "draft", "note": "Not sent. A person reviews and sends it."}


@server.tool(annotations=READ)
def list_drafts(vendor_id: str | None = None) -> dict:
    """List email drafts, newest first."""
    q = "SELECT * FROM email_drafts" + (" WHERE vendor_id=?" if vendor_id else "") + " ORDER BY created_at DESC"
    with db() as con:
        return {"drafts": rows(con.execute(q, (vendor_id,) if vendor_id else ()))}


if __name__ == "__main__":
    serve(server)
