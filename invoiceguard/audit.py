"""Append-only, tamper-evident audit log of every step: model calls, tool calls, checks, decisions, approvals.

Each row stores the SHA-256 of the previous row plus its own content, so editing or deleting any
past row breaks the chain, and `verify()` finds where. Approval tokens and keys are never logged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

_LOCK = threading.Lock()
SCHEMA = """CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, run_id TEXT, actor TEXT, kind TEXT NOT NULL,
    payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL)"""


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.execute(SCHEMA)

    def _con(self):
        return sqlite3.connect(self.path, timeout=10)

    def write(self, kind: str, payload: dict, run_id: str | None = None, actor: str | None = None) -> str:
        ts = datetime.now(UTC).isoformat(timespec="milliseconds")
        body = json.dumps(payload, sort_keys=True, default=str)
        with _LOCK, self._con() as con:
            row = con.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else "0" * 64
            h = _hash(prev, ts, run_id, actor, kind, body)
            con.execute(
                "INSERT INTO events (ts, run_id, actor, kind, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (ts, run_id, actor, kind, body, prev, h),
            )
        return h

    def read(self, run_id: str | None = None, limit: int = 500) -> list[dict]:
        with self._con() as con:
            q = "SELECT seq, ts, run_id, actor, kind, payload, hash FROM events"
            rows = con.execute(
                q + (" WHERE run_id=? ORDER BY seq" if run_id else " ORDER BY seq DESC LIMIT ?"),
                (run_id,) if run_id else (limit,),
            ).fetchall()
        return [
            dict(seq=r[0], ts=r[1], run_id=r[2], actor=r[3], kind=r[4], payload=json.loads(r[5]), hash=r[6])
            for r in rows
        ]

    def verify(self) -> dict:
        """Recompute the chain. Returns ok=True, or the first row that doesn't match."""
        with self._con() as con:
            rows = con.execute(
                "SELECT seq, ts, run_id, actor, kind, payload, prev_hash, hash FROM events ORDER BY seq"
            ).fetchall()
        prev = "0" * 64
        for seq, ts, run_id, actor, kind, body, prev_hash, h in rows:
            if prev_hash != prev or _hash(prev, ts, run_id, actor, kind, body) != h:
                return dict(ok=False, rows=len(rows), broken_at=seq)
            prev = h
        return dict(ok=True, rows=len(rows))


def _hash(prev, ts, run_id, actor, kind, body) -> str:
    return hashlib.sha256("|".join(map(str, (prev, ts, run_id, actor, kind, body))).encode()).hexdigest()
