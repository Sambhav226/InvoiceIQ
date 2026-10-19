"""Per-user run history, settings and source PDFs.

One small backend interface, two implementations: SQLite (local dev, tests, what-if previews) and Firestore
(production, INVOICEIQ_STORE=firestore). Every record carries `expire_at`; Firestore's TTL policy deletes expired
documents, and every read here hides them before that happens. All query logic lives once, in Python, on top.
"""
import contextvars
import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

DB = os.getenv("INVOICEIQ_DB", str(Path(__file__).resolve().parent.parent / "invoiceiq.db"))
MANUAL_MIN = 12   # minutes an AP clerk spends per invoice by hand (open, find PO, match, key in)
REVIEW_MIN = 4    # minutes a reviewer spends on a HOLD with the reasons already laid out
DAY = 86_400
RETENTION_CHOICES = (7, 30, 90)
DEFAULT_RETENTION, GUEST_RETENTION, GUEST_PROFILE_DAYS = 30, 1, 30
MAX_RUNS = {True: 50, False: 500}  # per guest / per account: bounds storage, per-request reads and lock hold time
MAX_PDF = 900_000  # a Firestore document is capped at 1 MiB; bigger PDFs are still processed, just not kept for replay


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


# ---------------------------------------------------------------- backends
class Sqlite:
    def __init__(self, path):
        self.path = path

    def _c(self):
        c = sqlite3.connect(self.path)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS runs  (uid TEXT, id INTEGER, row TEXT, expire_at REAL, PRIMARY KEY (uid, id));
            CREATE TABLE IF NOT EXISTS files (uid TEXT, id INTEGER, pdf BLOB, expire_at REAL, PRIMARY KEY (uid, id));
            CREATE TABLE IF NOT EXISTS users (uid TEXT PRIMARY KEY, doc TEXT, expire_at REAL);""")
        now = time.time()  # local stand-in for Firestore's TTL sweeper
        for t in ("runs", "files", "users"):
            c.execute(f"DELETE FROM {t} WHERE expire_at IS NOT NULL AND expire_at <= ?", (now,))
        return c

    def runs(self, uid):
        with self._c() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT row FROM runs WHERE uid=?", (uid,))]

    def put_run(self, uid, row):
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)", (uid, row["id"], json.dumps(row, default=str), row["expire_at"]))

    def put_file(self, uid, rid, pdf, exp):
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", (uid, rid, pdf, exp))

    def get_file(self, uid, rid):
        with self._c() as c:
            r = c.execute("SELECT pdf FROM files WHERE uid=? AND id=?", (uid, rid)).fetchone()
        return r[0] if r else None

    def expire_file(self, uid, rid, exp):
        with self._c() as c:
            c.execute("UPDATE files SET expire_at=? WHERE uid=? AND id=?", (exp, uid, rid))

    def user(self, uid):
        with self._c() as c:
            r = c.execute("SELECT doc FROM users WHERE uid=?", (uid,)).fetchone()
        return json.loads(r[0]) if r else None

    def put_user(self, uid, doc):
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO users VALUES (?,?,?)", (uid, json.dumps(doc), doc.get("expire_at")))

    def wipe(self, uid, profile):
        with self._c() as c:
            for t in ("runs", "files") + (("users",) if profile else ()):
                c.execute(f"DELETE FROM {t} WHERE uid=?", (uid,))


class Firestore:
    """users/{uid} = profile (retention, policy) · users/{uid}/runs/{id} · users/{uid}/files/{id}.
    TTL policies on the `expire_at` field of collection groups users, runs and files do the deleting."""

    def __init__(self):
        from google.cloud import firestore
        self.db = firestore.Client(project=os.getenv("GCP_PROJECT") or None)

    @staticmethod
    def _ts(t):
        return datetime.fromtimestamp(t, timezone.utc)

    def _u(self, uid):
        return self.db.collection("users").document(uid)

    def runs(self, uid):
        now = time.time()
        rows = (json.loads(d.get("row")) for d in self._u(uid).collection("runs").stream())
        return [r for r in rows if r["expire_at"] > now]

    def put_run(self, uid, row):
        self._u(uid).collection("runs").document(str(row["id"])).set(
            {"row": json.dumps(row, default=str), "expire_at": self._ts(row["expire_at"])})

    def put_file(self, uid, rid, pdf, exp):
        self._u(uid).collection("files").document(str(rid)).set({"pdf": pdf, "expire_at": self._ts(exp)})

    def get_file(self, uid, rid):
        s = self._u(uid).collection("files").document(str(rid)).get()
        return s.get("pdf") if s.exists and s.get("expire_at").timestamp() > time.time() else None

    def expire_file(self, uid, rid, exp):
        ref = self._u(uid).collection("files").document(str(rid))
        if ref.get().exists:
            ref.update({"expire_at": self._ts(exp)})

    def user(self, uid):
        s = self._u(uid).get()
        if not s.exists:
            return None
        d = s.to_dict()
        return None if d.get("expire_at") and d["expire_at"].timestamp() <= time.time() else json.loads(d["doc"])

    def put_user(self, uid, doc):
        self._u(uid).set({"doc": json.dumps(doc), **({"expire_at": self._ts(doc["expire_at"])} if doc.get("expire_at") else {})})

    def wipe(self, uid, profile):
        for sub in ("runs", "files"):
            for d in self._u(uid).collection(sub).stream():
                d.reference.delete()
        if profile:
            self._u(uid).delete()


_override = contextvars.ContextVar("store_override", default=None)
_main, _init_lock, _id_lock = None, threading.Lock(), threading.Lock()


def _b():
    global _main
    if _override.get():
        return _override.get()
    if _main is None:
        with _init_lock:
            _main = _main or (Firestore() if os.getenv("INVOICEIQ_STORE") == "firestore" else Sqlite(DB))
    return _main


@contextmanager
def scratch_db():
    """Isolated, throwaway SQLite history for what-if previews: real history, PO balances and Firestore untouched."""
    path = tempfile.mktemp(suffix=".db")
    token = _override.set(Sqlite(path))
    try:
        yield
    finally:
        _override.reset(token)
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------- users, retention, policy
def profile(uid: str, guest: bool = False) -> dict:
    """Create the profile on first sight; when a guest links a real account, lift guest retention to the default."""
    doc, now = _b().user(uid), time.time()
    if doc is None:
        doc = {"guest": guest, "retention_days": GUEST_RETENTION if guest else DEFAULT_RETENTION, "policy": None,
               "created_at": now, **({"expire_at": now + GUEST_PROFILE_DAYS * DAY} if guest else {})}
        _b().put_user(uid, doc)
    elif doc["guest"] and not guest:
        doc.update(guest=False)
        doc.pop("expire_at", None)
        _b().put_user(uid, doc)
        set_retention(uid, DEFAULT_RETENTION)
        doc = _b().user(uid)
    return doc


def _doc(uid):
    return _b().user(uid) or {"guest": False, "retention_days": DEFAULT_RETENTION, "policy": None}


def set_retention(uid: str, days: int) -> dict:
    """Changes how long this user's data lives, applied to everything already stored too."""
    doc = _doc(uid)
    doc["retention_days"] = days
    _b().put_user(uid, doc)
    for r in _b().runs(uid):
        r["expire_at"] = r["created_at"] + days * DAY
        _b().put_run(uid, r)
        _b().expire_file(uid, r["id"], r["expire_at"])
    return doc


def get_policy(uid: str = "local") -> Policy:
    p = _doc(uid).get("policy")
    return Policy(**p) if p else Policy()


def set_policy(uid: str, p: Policy) -> Policy:
    doc = _doc(uid)
    doc["policy"] = p.model_dump()
    _b().put_user(uid, doc)
    return p


# ---------------------------------------------------------------- runs
class LimitReached(Exception):
    pass


def create(uid: str, filename: str, sha256: str) -> int:
    doc = _doc(uid)
    with _id_lock:  # ponytail: per-process lock; fine at --max-instances 1, a Firestore counter if it scales out
        rows = _b().runs(uid)
        if len(rows) >= MAX_RUNS[bool(doc.get("guest"))]:
            raise LimitReached(f"You've reached {len(rows)} stored runs. Delete some data in Data & privacy to continue.")
        rid = max((r["id"] for r in rows), default=0) + 1
        now, days = time.time(), doc["retention_days"]
        _b().put_run(uid, {"id": rid, "created_at": now, "expire_at": now + days * DAY, "filename": filename, "sha256": sha256,
                           "decision": None, "vendor_id": None, "vendor_name": None, "invoice_number": None, "inv_key": None,
                           "fy": None, "po_number": None, "base": None, "total": None, "high_risk": 0, "duration_s": None,
                           "cost_usd": 0, "cached": 0, "review": None, "data": None})
    return rid


def check_room(uid: str, guest: bool):
    if len(_b().runs(uid)) >= MAX_RUNS[guest]:
        raise LimitReached("You've reached the limit of stored runs. Delete some data in Data & privacy to continue.")


def _raw(uid, rid):
    return next((r for r in _b().runs(uid) if r["id"] == rid), None)


def finish(uid: str, rid: int, **fields):
    row = _raw(uid, rid)
    if row is None:  # user deleted their data while this run was in flight: don't resurrect it
        return
    row.update(fields)
    _b().put_run(uid, row)


def get(uid: str, rid: int):
    return _raw(uid, rid)


def _done(uid):
    return sorted((r for r in _b().runs(uid) if r["decision"]), key=lambda r: -r["id"])


def listing(uid: str):
    out = []
    for r in _done(uid):
        reasons = ((r.get("data") or {}).get("decision") or {}).get("reasons", [])
        out.append({**{k: v for k, v in r.items() if k != "data"}, "reason": next((x["text"] for x in reasons if x["severity"] != "note"), "")})
    return out


def _standing(r):
    """Counts against PO balance / duplicates: approved, or held and not rejected by a reviewer."""
    return r["decision"] == "APPROVE" or (r["decision"] == "HOLD" and not (r["review"] and r["review"]["action"] == "reject"))


def reserved_on_po(uid: str, po: str, exclude: int) -> float:
    return sum(r["base"] or 0 for r in _done(uid) if r["po_number"] == po and r["id"] != exclude and _standing(r))


def _first(uid, exclude, test):
    return next((r for r in sorted(_done(uid), key=lambda r: r["id"]) if r["id"] != exclude and test(r)), None)


def find_same_file(uid: str, sha: str, exclude: int):
    return _first(uid, exclude, lambda r: r["sha256"] == sha)


def find_same_invoice(uid: str, vendor_id: str, key: str, fy: str, exclude: int):
    """Same vendor + normalised number (+ financial year when known), among standing runs."""
    return _first(uid, exclude, lambda r: _standing(r) and r["vendor_id"] == vendor_id and r["inv_key"] == key and (not fy or r["fy"] == fy))


def find_rebill(uid: str, vendor_id: str, po: str, total: float, exclude: int):
    """Same vendor + PO + amount as an already-approved run: a re-bill under a new number."""
    return _first(uid, exclude, lambda r: r["decision"] == "APPROVE" and r["vendor_id"] == vendor_id and r["po_number"] == po
                  and r["total"] is not None and abs(r["total"] - total) <= 1)


def review(uid: str, rid: int, action: str, note: str):
    row = _raw(uid, rid)
    if row and row["decision"] == "HOLD":
        row["review"] = {"action": action, "note": note, "by": "AP reviewer", "at": time.time()}
        _b().put_run(uid, row)
    return get(uid, rid)


def save_pdf(uid: str, rid: int, pdf: bytes):
    row = _raw(uid, rid)
    if row and len(pdf) <= MAX_PDF:
        _b().put_file(uid, rid, pdf, row["expire_at"])


def get_pdf(uid: str, rid: int):
    return _b().get_file(uid, rid)


def reset(uid: str = "local"):
    """Clears run history (demo reset). Profile, policy and retention stay."""
    _b().wipe(uid, profile=False)


def delete_all(uid: str):
    """Everything this user has: runs, PDFs, policy, profile."""
    _b().wipe(uid, profile=True)


def metrics(uid: str = "local"):
    rows = listing(uid)
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
