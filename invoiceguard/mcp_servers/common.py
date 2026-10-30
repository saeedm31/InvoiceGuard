"""Shared bits for the MCP servers: data folder, database, tool annotations, transport choice."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from mcp.types import ToolAnnotations

from ..config import ROOT

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
MONEY = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


def data_dir() -> Path:
    return Path(os.getenv("IG_DATA_DIR", ROOT / "data"))


def db() -> sqlite3.Connection:
    con = sqlite3.connect(data_dir() / "erp.db", timeout=10)
    con.row_factory = sqlite3.Row
    return con


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def mask_iban(iban: str | None) -> str | None:
    if not iban:
        return None
    s = iban.replace(" ", "")
    return f"{s[:4]} **** **** {s[-4:]}"


def serve(server) -> None:
    """stdio by default (the app starts each server as a child process); `--http PORT` for Docker."""
    if "--http" in sys.argv:
        from mcp.server.transport_security import TransportSecuritySettings

        port = int(sys.argv[sys.argv.index("--http") + 1])
        hosts = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*").split(",") if h.strip()]
        sec = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts)
        server.run("streamable-http", host="0.0.0.0", port=port, transport_security=sec)
    else:
        server.run()
