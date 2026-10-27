"""SQLite storage: vendors, invoices, approval requests and an append-only audit log.

SQLite keeps the demo zero-setup. The SQL is plain enough to move to Postgres/Snowflake
in a real deployment (see docs/DESIGN.md).
"""

from __future__ import annotations

import csv
import difflib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    tax_id TEXT,
    approved INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    vendor_name TEXT,
    invoice_number TEXT,
    total REAL,
    currency TEXT,
    data_json TEXT,
    issues_json TEXT,
    confidence REAL,
    pii_masked_json TEXT,
    decision_reason TEXT,
    agent_trace_json TEXT,
    extraction_meta_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_vendor_number ON invoices(vendor_name, invoice_number);
CREATE TABLE IF NOT EXISTS approval_requests (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    invoice_id INTEGER,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
);
"""

_SUFFIXES = re.compile(r"\b(inc|llc|ltd|co|corp|corporation|company|limited|plc)\b\.?")


def normalize_vendor(name: str | None) -> str:
    if not name:
        return ""
    n = name.lower().replace("&", " and ")
    n = _SUFFIXES.sub(" ", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def normalize_invoice_number(num: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", num or "").upper()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str | Path = ":memory:"):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---------- vendors ----------
    def seed_vendors(self, csv_path: str | Path) -> int:
        n = 0
        with open(csv_path, newline="") as f:
            for row in csv.DictReader(f):
                self.conn.execute(
                    "INSERT OR IGNORE INTO vendors(name, normalized_name, tax_id, approved) VALUES (?,?,?,?)",
                    (row["name"], normalize_vendor(row["name"]), row.get("tax_id") or None,
                     int(row.get("approved", "1") or 1)),
                )
                n += 1
        self.conn.commit()
        return n

    def lookup_vendor(self, name: str) -> dict[str, Any]:
        norm = normalize_vendor(name)
        row = self.conn.execute("SELECT * FROM vendors WHERE normalized_name=?", (norm,)).fetchone()
        if row:
            return {"found": True, "vendor": dict(row)}
        all_names = [r["normalized_name"] for r in self.conn.execute("SELECT normalized_name FROM vendors")]
        close = difflib.get_close_matches(norm, all_names, n=3, cutoff=0.8)
        suggestions = [
            dict(self.conn.execute("SELECT * FROM vendors WHERE normalized_name=?", (c,)).fetchone())
            for c in close
        ]
        return {"found": False, "close_matches": suggestions}

    # ---------- invoices ----------
    def find_by_hash(self, file_hash: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM invoices WHERE file_hash=?", (file_hash,)).fetchone()
        return dict(row) if row else None

    def find_duplicates(self, vendor_name: str | None, invoice_number: str | None,
                        exclude_id: int | None = None) -> list[dict]:
        """Same vendor + same invoice number = duplicate, regardless of file or layout."""
        vn, num = normalize_vendor(vendor_name), normalize_invoice_number(invoice_number)
        if not vn or not num:
            return []
        out = []
        for row in self.conn.execute("SELECT id, vendor_name, invoice_number, total, status, file_name FROM invoices"):
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            if normalize_vendor(row["vendor_name"]) == vn and normalize_invoice_number(row["invoice_number"]) == num:
                out.append(dict(row))
        return out

    def insert_invoice(self, *, file_name: str, file_hash: str, status: str, data: dict | None,
                       issues: list[str], confidence: float, pii_counts: dict,
                       extraction_meta: dict) -> int:
        now = _now()
        data = data or {}
        cur = self.conn.execute(
            """INSERT INTO invoices(file_name, file_hash, status, vendor_name, invoice_number, total, currency,
                   data_json, issues_json, confidence, pii_masked_json, extraction_meta_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (file_name, file_hash, status, data.get("vendor_name"), data.get("invoice_number"),
             data.get("total"), data.get("currency"), json.dumps(data), json.dumps(issues), confidence,
             json.dumps(pii_counts), json.dumps(extraction_meta), now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_decision(self, invoice_id: int, status: str, reason: str, trace: list | None = None) -> None:
        self.conn.execute(
            "UPDATE invoices SET status=?, decision_reason=?, agent_trace_json=?, updated_at=? WHERE id=?",
            (status, reason, json.dumps(trace or []), _now(), invoice_id),
        )
        self.conn.commit()

    def get_invoice(self, invoice_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return dict(row) if row else None

    def list_invoices(self, status: str | list[str] | None = None) -> list[dict]:
        if status is None:
            rows = self.conn.execute("SELECT * FROM invoices ORDER BY id DESC")
        else:
            statuses = [status] if isinstance(status, str) else status
            q = f"SELECT * FROM invoices WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id DESC"
            rows = self.conn.execute(q, statuses)
        return [dict(r) for r in rows]

    # ---------- approvals & audit ----------
    def add_approval_request(self, invoice_id: int, reason: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO approval_requests(invoice_id, reason, created_at) VALUES (?,?,?)",
            (invoice_id, reason, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_human_decision(self, invoice_id: int, approved: bool, reviewer: str, comment: str) -> None:
        status = "approved" if approved else "rejected"
        self.conn.execute(
            "UPDATE approval_requests SET status=? WHERE invoice_id=? AND status='pending'",
            (status, invoice_id),
        )
        self.conn.execute("UPDATE invoices SET status=?, updated_at=? WHERE id=?", (status, _now(), invoice_id))
        self.conn.commit()
        self.log(invoice_id, f"human:{reviewer}", f"human_{status}", comment)

    def log(self, invoice_id: int | None, actor: str, event: str, detail: Any = None) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        self.conn.execute(
            "INSERT INTO audit_log(ts, invoice_id, actor, event, detail) VALUES (?,?,?,?,?)",
            (_now(), invoice_id, actor, event, detail),
        )
        self.conn.commit()

    def audit_trail(self, invoice_id: int | None = None, limit: int = 500) -> list[dict]:
        if invoice_id is None:
            rows = self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.conn.execute("SELECT * FROM audit_log WHERE invoice_id=? ORDER BY id", (invoice_id,))
        return [dict(r) for r in rows]
