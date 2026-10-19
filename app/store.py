"""Run history in SQLite (stdlib). One row per run; full trace kept as JSON."""
import contextvars
import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

DB = os.getenv("INVOICEIQ_DB", str(Path(__file__).resolve().parent.parent / "invoiceiq.db"))
MANUAL_MIN = 12   # minutes an AP clerk spends per invoice by hand (open, find PO, match, key in)
REVIEW_MIN = 4    # minutes a reviewer spends on a HOLD with the reasons already laid out


_db_path = contextvars.ContextVar("db_path", default=DB)


class Policy(BaseModel):
    """AP policy an ops team can tune from the UI. Bounds are enforced here (trust boundary)."""
    po_tolerance_pct: float = Field(2.0, ge=0, le=50)          # % of invoice value allowed over remaining PO balance
    po_tolerance_cap: float = Field(1000, ge=0, le=10_000_000)  # ...capped at this many rupees
    scan_confidence_min: float = Field(0.85, ge=0.5, le=1)     # scanned docs below this go to a human
    vendor_name_match_min: float = Field(0.85, ge=0.6, le=1)   # fuzzy name match when GSTIN is absent
    ap_manager_limit: float = Field(100_000, ge=0)             # sign-off: AP Manager up to this total
    controller_limit: float = Field(1_000_000, ge=0)           # Finance Controller up to this, CFO above
    approve_inferred_po: bool = True                           # off = invoices without a PO number always HOLD

    @model_validator(mode="after")
    def _limits_ordered(self):
        if self.controller_limit < self.ap_manager_limit:
            raise ValueError("Finance Controller limit must be at least the AP Manager limit")
        return self


def _db():
    c = sqlite3.connect(_db_path.get())
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, filename TEXT, sha256 TEXT,
        decision TEXT, vendor_id TEXT, vendor_name TEXT, invoice_number TEXT, inv_key TEXT, fy TEXT,
        po_number TEXT, base REAL, total REAL, high_risk INTEGER DEFAULT 0, duration_s REAL,
        cost_usd REAL DEFAULT 0, cached INTEGER DEFAULT 0, review TEXT, data TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    return c


@contextmanager
def scratch_db():
    """Isolated, throwaway history for what-if previews: real history and PO balances stay untouched."""
    path = tempfile.mktemp(suffix=".db")
    token = _db_path.set(path)
    try:
        yield
    finally:
        _db_path.reset(token)
        if os.path.exists(path):
            os.remove(path)


def get_policy() -> Policy:
    with _db() as c:
        r = c.execute("SELECT value FROM settings WHERE key='policy'").fetchone()
    return Policy.model_validate_json(r[0]) if r else Policy()


def set_policy(p: Policy) -> Policy:
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('policy', ?)", (p.model_dump_json(),))
    return p


def create(filename: str, sha256: str) -> int:
    with _db() as c:
        return c.execute("INSERT INTO runs (created_at, filename, sha256) VALUES (?,?,?)",
                         (time.time(), filename, sha256)).lastrowid


def finish(run_id: int, **f):
    f["data"] = json.dumps(f["data"], default=str)
    with _db() as c:
        c.execute(f"UPDATE runs SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), run_id))


def _row(r):
    d = dict(r)
    d["data"] = json.loads(d["data"]) if d["data"] else None
    d["review"] = json.loads(d["review"]) if d["review"] else None
    return d


def get(run_id: int):
    with _db() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _row(r) if r else None


def listing():
    with _db() as c:
        rows = c.execute("SELECT * FROM runs WHERE decision IS NOT NULL ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = _row(r)
        reasons = (d.pop("data") or {}).get("decision", {}).get("reasons", [])
        d["reason"] = next((x["text"] for x in reasons if x["severity"] != "note"), "")
        out.append(d)
    return out


# A run "stands" (counts against PO balance / duplicates) if approved, or held and not yet rejected by a reviewer.
STANDING = "(decision='APPROVE' OR (decision='HOLD' AND (review IS NULL OR review NOT LIKE '%\"reject\"%')))"


def reserved_on_po(po: str, exclude: int) -> float:
    with _db() as c:
        return c.execute(f"SELECT COALESCE(SUM(base),0) FROM runs WHERE po_number=? AND id<>? AND {STANDING}",
                         (po, exclude)).fetchone()[0]


def find(where: str, args: tuple, exclude: int):
    with _db() as c:
        r = c.execute(f"SELECT * FROM runs WHERE id<>? AND decision IS NOT NULL AND {where} ORDER BY id LIMIT 1",
                      (exclude, *args)).fetchone()
    return dict(r) if r else None


def review(run_id: int, action: str, note: str):
    rec = {"action": action, "note": note, "by": "AP reviewer", "at": time.time()}
    with _db() as c:
        c.execute("UPDATE runs SET review=? WHERE id=? AND decision='HOLD'", (json.dumps(rec), run_id))
    return get(run_id)


def reset():
    with _db() as c:
        c.execute("DELETE FROM runs")
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():  # exists only after first insert
            c.execute("DELETE FROM sqlite_sequence WHERE name='runs'")


def metrics():
    rows = listing()
    n = len(rows)
    by = lambda d: [r for r in rows if r["decision"] == d]
    appr, hold, rej = by("APPROVE"), by("HOLD"), by("REJECT")
    pending = [r for r in hold if not r["review"]]
    reviewed = [r for r in hold if r["review"]]
    hours = (n * MANUAL_MIN - len(hold) * REVIEW_MIN) / 60
    return {
        "processed": n,
        "auto_approval_rate": round(len(appr) / n * 100, 1) if n else 0,
        "on_hold_value": sum(r["total"] or 0 for r in pending),
        "protected_value": sum(r["total"] or 0 for r in rej + [r for r in hold if r["high_risk"]]),
        "pending_review": len(pending),
        "override_rate": round(sum(1 for r in reviewed if r["review"]["action"] == "approve") / len(reviewed) * 100, 1) if reviewed else 0,
        "avg_seconds": round(sum(r["duration_s"] or 0 for r in rows) / n, 1) if n else 0,
        "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
        "cost_per_invoice_usd": round(sum(r["cost_usd"] or 0 for r in rows) / n, 4) if n else 0,
        "hours_saved": round(max(hours, 0), 1),
        "hold_rate": round(len(hold) / n, 3) if n else None,
        "manual_min": MANUAL_MIN, "review_min": REVIEW_MIN,
        "counts": {"APPROVE": len(appr), "HOLD": len(hold), "REJECT": len(rej)},
        "formulas": {
            "auto_approval_rate": "APPROVE ÷ all invoices processed",
            "on_hold_value": "Σ invoice total of HOLDs not yet reviewed",
            "protected_value": "Σ total of REJECTs (duplicates, blocked vendors) + HIGH-RISK holds (bank-change)",
            "hours_saved": f"(invoices × {MANUAL_MIN} min manual − HOLDs × {REVIEW_MIN} min review) ÷ 60",
            "override_rate": "HOLDs a reviewer approved ÷ HOLDs reviewed. High = rules too strict",
            "cost_per_invoice_usd": "Gemini tokens × list price, averaged",
        },
    }
