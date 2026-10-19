"""The 7-stage invoice pipeline. Only stage 2 uses an LLM; everything that decides is deterministic.
Rules live in .claude/skills/ap-decision-rules/SKILL.md."""
import csv
import difflib
import hashlib
import io
import json
import os
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path

from pypdf import PdfReader

from . import store
from .extract import extract

DATA = Path(__file__).resolve().parent.parent / "data"
BUYER_STATE = "29"  # Zephyr Retail, Karnataka
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
SUFFIXES = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "company"}
PACE = float(os.getenv("INVOICEIQ_PACE", "0.35"))  # UI pacing between stages so humans can follow; not counted in stage timings
STAGES = ["Ingest", "Extract", "Validate", "Vendor", "PO match", "Duplicate", "Decide"]
RANK = {"ok": 0, "hold": 1, "reject": 2}
# ponytail: one global lock serialises PO-balance + duplicate checks with the final write; per-vendor locks if throughput matters
DECIDE_LOCK = threading.Lock()


def inr(x) -> str:
    if x is None:
        return "—"
    neg, x = x < 0, abs(round(x))
    s = str(int(x))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        s = ",".join([head[max(i - 2, 0):i] for i in range(len(head), 0, -2)][::-1]) + "," + tail
    return ("-₹" if neg else "₹") + s


def alnum(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def norm_inv(s) -> str:
    return re.sub(r"(?<!\d)0+(?=\d)", "", alnum(s))  # INV-0042 / inv 42 -> INV42; INV-100 stays INV100


def norm_name(s) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()
    return " ".join(w for w in words if w not in SUFFIXES)


def fy(d: date | None) -> str:
    if not d:
        return ""
    y = d.year if d.month >= 4 else d.year - 1
    return f"FY{y % 100:02d}-{(y + 1) % 100:02d}"


def parse_date(s):
    s = (s or "").strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    for f in ("%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def load_master():
    vendors = json.loads((DATA / "vendors.json").read_text())
    with open(DATA / "purchase_orders.csv") as f:
        pos = [{**r, "amount": float(r["amount"]), "tax_rate": float(r["tax_rate"]),
                "billed_to_date": float(r["billed_to_date"])} for r in csv.DictReader(f)]
    with open(DATA / "paid_invoices.csv") as f:
        paid = [{**r, "total": float(r["total"])} for r in csv.DictReader(f)]
    return vendors, pos, paid


def result(name, findings, evidence, ok_summary):
    """findings: list of (severity, text). severity 'ok' = warning that doesn't block."""
    worst = max((RANK[s] for s, _ in findings), default=0)
    sev = [k for k, v in RANK.items() if v == worst][0]
    status = "fail" if worst else ("warn" if findings else "pass")
    summary = " ".join(t for _, t in sorted(findings, key=lambda f: -RANK[f[0]])) or ok_summary
    return {"stage": name, "status": status, "severity": sev, "summary": summary,
            "findings": [{"severity": s, "text": t} for s, t in findings], "evidence": evidence}


# ---------------------------------------------------------------- stages
def s_ingest(ctx):
    pdf = ctx["pdf"]
    if not pdf.startswith(b"%PDF"):
        return result("Ingest", [("hold", "File is not a PDF.")], {}, ""), True
    try:
        reader = PdfReader(io.BytesIO(pdf))
        text = "\n".join(p.extract_text() or "" for p in reader.pages[:10])
        pages = len(reader.pages)
    except Exception as e:
        return result("Ingest", [("hold", f"PDF could not be opened ({e}).")], {}, ""), True
    ctx["text"], ctx["digital"] = text, len(text.strip()) > 50
    f = [("hold", f"{pages} pages; only the first 10 are read.")] if pages > 10 else []
    kind = "digital (text layer found)" if ctx["digital"] else "scanned image (no text layer → vision extraction)"
    return result("Ingest", f, {"sha256": ctx["sha"][:16] + "…", "pages": pages, "type": kind, "size_kb": round(len(pdf) / 1024, 1)},
                  f"{pages}-page {kind} PDF received."), False


def s_extract(ctx):
    try:
        inv, usage = extract(ctx["pdf"], ctx["sha"], cached_only=ctx["cached_only"])
    except Exception as e:
        return result("Extract", [("hold", f"Extraction failed after retry ({type(e).__name__}). Needs manual keying.")],
                      {"error": str(e)[:300]}, ""), True
    ctx["inv"], ctx["usage"] = inv, usage
    f = []  # cached/mock extraction is shown as a header badge, not as a reason
    filled = sum(v not in (None, [], "") for v in inv.model_dump().values())
    ev = {"vendor": inv.vendor_name, "invoice_no": inv.invoice_number, "date": inv.invoice_date, "po_ref": inv.po_reference,
          "lines": len(inv.line_items), "total": inr(inv.total), "model": usage["model"], "confidence": inv.confidence,
          "tokens": f"{usage['input_tokens']} in / {usage['output_tokens']} out", "cost": f"${usage['cost_usd']:.4f}",
          "source": {"mock": "cached extraction (mock mode)", "fallback": "cached extraction (live model unavailable)"}.get(usage["cached"], "live model call")}
    if inv.notes:
        ev["model_notes"] = "; ".join(inv.notes)
    return result("Extract", f, ev, f"Read {filled} fields: {inv.vendor_name or '?'}, invoice {inv.invoice_number or '?'}, "
                                    f"total {inr(inv.total)} ({len(inv.line_items)} line items)."), False


def s_validate(ctx):
    inv, f, ev = ctx["inv"], [], {}
    for k, label in (("vendor_name", "vendor name"), ("invoice_number", "invoice number"), ("invoice_date", "invoice date"), ("total", "total")):
        if getattr(inv, k) in (None, ""):
            f.append(("hold", f"Missing {label} on the invoice."))
    if inv.total is not None and inv.total <= 0:
        f.append(("hold", f"Total is {inr(inv.total)}. Credit notes and zero-value invoices are not auto-processed; adjust against the original invoice manually."))
    d = parse_date(inv.invoice_date)
    ctx["date"] = d
    if inv.invoice_date and not d:
        f.append(("hold", f"Invoice date '{inv.invoice_date}' is not a valid date."))
    elif d and d > date.today():
        f.append(("hold", f"Invoice is dated in the future ({d})."))

    for i, li in enumerate(inv.line_items, 1):
        if None not in (li.quantity, li.unit_price, li.amount) and abs(li.quantity * li.unit_price - li.amount) > 1:
            diff = li.amount - li.quantity * li.unit_price
            f.append(("hold", f"Line {i}: {li.quantity:g} × {inr(li.unit_price)} = {inr(li.quantity * li.unit_price)}, but the invoice says "
                              f"{inr(li.amount)} ({inr(abs(diff))} {'over' if diff > 0 else 'under'}-billed)."))
    line_sum = round(sum(li.amount or 0 for li in inv.line_items), 2)
    tax = inv.total_tax if inv.total_tax is not None else sum(x or 0 for x in (inv.cgst, inv.sgst, inv.igst))
    has_breakup = any(x for x in (inv.cgst, inv.sgst, inv.igst))
    rate = inv.tax_rate or 0.18
    rate = rate / 100 if rate > 1 else rate  # model sometimes returns 18 instead of 0.18

    if inv.tax_inclusive and not has_breakup and inv.total:
        base = round(inv.total / (1 + rate), 2)
        f.append(("hold", f"Priced 'inclusive of GST' with no CGST/SGST/IGST breakup, so this is not a valid tax invoice (CGST Rule 46) "
                          f"and input tax credit of {inr(inv.total - base)} is at risk. Request a proper tax invoice."))
        ev["taxable_value (back-computed)"] = inr(base)
    else:
        base = inv.subtotal if inv.subtotal is not None else line_sum
        if inv.line_items and inv.subtotal is not None and abs(line_sum - inv.subtotal) > 1:
            f.append(("hold", f"Line items add up to {inr(line_sum)} but subtotal says {inr(inv.subtotal)}."))
        if inv.total is not None and base is not None and abs(base + tax - inv.total) > 1:
            f.append(("hold", f"Subtotal {inr(base)} + tax {inr(tax)} = {inr(base + tax)}, but invoice total is {inr(inv.total)}."))
        ev.update({"line_sum": inr(line_sum), "subtotal": inr(base), "tax": inr(tax), "total": inr(inv.total)})
    ctx["base"] = base

    g = alnum(inv.vendor_gstin or "")
    if not g:
        f.append(("hold", "No supplier GSTIN on the invoice."))
    elif not GSTIN_RE.match(g):
        f.append(("hold", f"Supplier GSTIN '{inv.vendor_gstin}' is not a valid GSTIN format."))
    elif has_breakup:
        intra = g[:2] == BUYER_STATE
        if intra and (inv.igst or 0) > 0:
            f.append(("hold", "IGST charged on an intra-state supply (both parties in Karnataka). Should be CGST + SGST."))
        if not intra and ((inv.cgst or 0) + (inv.sgst or 0)) > 0:
            f.append(("hold", f"CGST/SGST charged on an inter-state supply (vendor state {g[:2]}). Should be IGST."))
        ev["gst_type"] = "intra-state (CGST+SGST)" if intra else "inter-state (IGST)"

    if ctx["digital"]:
        text = alnum(ctx["text"])
        checks = {"invoice no.": [inv.invoice_number], "GSTIN": [inv.vendor_gstin],
                  "total": [f"{inv.total:.2f}", f"{inv.total:.0f}"] if inv.total else []}
        miss = [k for k, vals in checks.items() if vals and vals[0] and not any(alnum(v) in text for v in vals)]
        for k in miss:
            f.append(("hold", f"Extracted {k} not found in the document text. Possible misread, so verify manually."))
        ev["grounding"] = "all key values found in source text" if not miss else f"not grounded: {', '.join(miss)}"
    else:
        cmin = ctx["policy"].scan_confidence_min
        ev["grounding"] = f"scanned: model confidence {inv.confidence:.2f} (min {cmin:.2f})"
        if inv.confidence < cmin:
            f.append(("hold", f"Scanned document read with low confidence ({inv.confidence:.2f}). Verify key fields."))
    return result("Validate", f, ev, f"Maths, GST and required fields check out. Key values are grounded in the document."), False


def s_vendor(ctx):
    inv, vendors = ctx["inv"], ctx["vendors"]
    g = alnum(inv.vendor_gstin or "")
    v = next((x for x in vendors if g and x["gstin"] == g), None)
    how = "GSTIN"
    if not v:
        scored = [(difflib.SequenceMatcher(None, norm_name(inv.vendor_name), norm_name(x["name"])).ratio(), x) for x in vendors]
        score, best = max(scored, key=lambda t: t[0])
        v, how = (best, f"name similarity {score:.2f}") if score >= ctx["policy"].vendor_name_match_min else (None, "")
    if not v:
        return result("Vendor", [("hold", f"'{inv.vendor_name}' is not in the vendor master. Route to vendor onboarding before payment.")],
                      {"invoice_vendor": inv.vendor_name, "gstin": inv.vendor_gstin}, ""), True
    ctx["vendor"] = v
    ev = {"matched": f"{v['id']} · {v['name']}", "matched_by": how, "status": v["status"]}
    if v["status"] == "blocked":
        return result("Vendor", [("reject", f"Vendor {v['name']} is blocked ({v['contact']}).")], ev, ""), True
    f = []
    if norm_name(inv.vendor_name) != norm_name(v["name"]):
        f.append(("ok", f"Name on invoice '{inv.vendor_name}' differs from master '{v['name']}'."))
    acct, ifsc = re.sub(r"\D", "", inv.bank_account or ""), alnum(inv.ifsc or "")
    ev["bank_on_invoice"] = f"{acct or '—'} / {ifsc or '—'}"
    ev["bank_in_master"] = f"{v['bank_account']} / {v['ifsc']}"
    if not acct:
        f.append(("ok", "No bank details on invoice. Payment goes to the master account."))
    elif acct != v["bank_account"] or (ifsc and ifsc != v["ifsc"]):
        ctx["high_risk"] = True
        f.append(("hold", f"Bank details changed: invoice asks for payment to account …{acct[-4:]} ({ifsc or 'no IFSC'}), but our records say "
                          f"…{v['bank_account'][-4:]} ({v['ifsc']}). This is the classic bank-change fraud pattern. Payment blocked until verified by phone."))
    return result("Vendor", f, ev, f"Approved vendor {v['name']} (matched by {how}). Bank details match the master."), False


def s_po(ctx):
    inv, v, base = ctx["inv"], ctx["vendor"], ctx.get("base")
    pos = ctx["pos"]
    if base is None:
        return result("PO match", [("hold", "No taxable value available to match against a PO.")], {}, ""), False

    def remaining(po):
        return round(po["amount"] - po["billed_to_date"] - store.reserved_on_po(po["po_number"], ctx["run_id"]), 2)

    P = ctx["policy"]
    tol = min(P.po_tolerance_pct / 100 * base, P.po_tolerance_cap)
    f, ev = [], {"invoice_taxable_value": inr(base), "tolerance": f"{inr(tol)} ({P.po_tolerance_pct:g}% capped at {inr(P.po_tolerance_cap)})"}
    if inv.po_reference:
        po = next((p for p in pos if alnum(p["po_number"]) == alnum(inv.po_reference)), None)
        if not po:
            return result("PO match", [("hold", f"PO '{inv.po_reference}' not found in the PO book. Possible typo; confirm with requester.")], ev, ""), False
        how = "explicit reference on invoice"
    else:
        open_pos = [p for p in pos if p["vendor_id"] == v["id"] and p["status"] == "open"]
        fits = [p for p in open_pos if base <= remaining(p) + tol]
        ev["open_pos_for_vendor"] = ", ".join(f"{p['po_number']} (left {inr(remaining(p))})" for p in open_pos) or "none"
        if len(fits) != 1:
            why = "none has enough balance" if not fits else f"{len(fits)} could fit ({', '.join(p['po_number'] for p in fits)})"
            return result("PO match", [("hold", f"No PO number on invoice and {why}. Ask the requester which PO this is for.")], ev, ""), False
        po, how = fits[0], "inferred: no PO on invoice; only open PO of this vendor that fits"
        inferred = f"No PO number on the invoice. Inferred {po['po_number']} ({po['description']}), the only open PO of this vendor that fits."
        f.append(("ok", inferred) if P.approve_inferred_po else ("hold", inferred + " Policy requires a PO reference, so confirm it with the requester."))
    if po["vendor_id"] == v["id"]:  # never reserve balance on another vendor's PO
        ctx["po"] = po["po_number"]
    rem = remaining(po)
    ev.update({"po": f"{po['po_number']} · {po['description']}", "matched_by": how, "po_amount": inr(po["amount"]),
               "billed_before": inr(po["amount"] - rem), "remaining_before": inr(rem), "remaining_after": inr(rem - base)})
    if po["vendor_id"] != v["id"]:
        f.append(("hold", f"{po['po_number']} belongs to a different vendor ({po['vendor_id']}). Likely a wrong PO reference."))
    elif po["status"] != "open":
        f.append(("hold", f"{po['po_number']} is {po['status']} (fully billed). Needs a PO amendment or a new PO."))
    elif base > rem + tol:
        f.append(("hold", f"Taxable value {inr(base)} exceeds remaining PO balance {inr(rem)} by {inr(base - rem)} "
                          f"(PO {inr(po['amount'])}, already billed {inr(po['amount'] - rem)}). Over-billing."))
    return result("PO match", f, ev, f"Matched {po['po_number']}: {inr(base)} against {inr(rem)} remaining; "
                                     f"{inr(rem - base)} left after this invoice."), False


def s_duplicate(ctx):
    inv, v, d = ctx["inv"], ctx["vendor"], ctx.get("date")
    key, ctx["fy"] = norm_inv(inv.invoice_number or ""), fy(d)
    f, ev = [], {"normalised_key": f"{v['id']}·{key}·{fy(d) or 'FY?'}", "checked": f"{len(ctx['paid'])} paid invoices + run history",
                 "this_invoice": f"{inv.invoice_number} · {ctx.get('po') or 'no PO'} · {inr(inv.total)}"}
    hit = store.find("sha256=?", (ctx["sha"],), ctx["run_id"])
    if hit:
        f.append(("reject", f"Identical file already processed as run #{hit['id']} ({hit['decision']})."))
    for p in ctx["paid"]:
        if p["vendor_id"] != v["id"]:
            continue
        same_no = key and norm_inv(p["invoice_number"]) == key and fy(parse_date(p["invoice_date"])) == ctx["fy"]
        same_bill = p["po_number"] == ctx.get("po") and inv.total is not None and abs(p["total"] - inv.total) <= 1
        if same_no or same_bill:
            ev["matched_paid_invoice"] = f"{p['invoice_number']} · {p['po_number']} · {inr(p['total'])} · paid {p['paid_on']}"
        if same_no:
            f.append(("reject", f"Invoice {p['invoice_number']} ({ctx['fy'] or 'same number'}) was already paid on {p['paid_on']}."))
        elif same_bill:
            f.append(("reject", f"Same vendor, same PO {p['po_number']} and same amount {inr(p['total'])} as {p['invoice_number']}, "
                                f"paid on {p['paid_on']}. Likely re-billed under a new number."))
    if not hit and key:
        # FY unknown (unparseable date) -> match on vendor + number alone
        q, args = ("vendor_id=? AND inv_key=? AND fy=?", (v["id"], key, ctx["fy"])) if ctx["fy"] else ("vendor_id=? AND inv_key=?", (v["id"], key))
        prior = store.find(f"{q} AND {store.STANDING}", args, ctx["run_id"])
        if prior:
            f.append(("reject", f"Invoice {inv.invoice_number} from this vendor already processed in run #{prior['id']} ({prior['decision']})."))
    if not hit and ctx.get("po") and inv.total is not None:
        prior = store.find("vendor_id=? AND po_number=? AND abs(total-?)<=1 AND decision='APPROVE'", (v["id"], ctx["po"], inv.total), ctx["run_id"])
        if prior:
            f.append(("reject", f"Same vendor, PO {ctx['po']} and amount {inr(inv.total)} as already-approved invoice "
                                f"{prior['invoice_number']} (run #{prior['id']}). Likely re-billed under a new number."))
    return result("Duplicate", f, ev, "No duplicate found: hash, invoice number (FY-aware) and PO+amount are all unique."), False


def decide(ctx, stages):
    inv, v = ctx.get("inv"), ctx.get("vendor")
    worst = max((RANK[s["severity"]] for s in stages), default=0)
    decision = ["APPROVE", "HOLD", "REJECT"][worst]
    reasons = sorted([(RANK[fd["severity"]], s["stage"], fd["text"]) for s in stages for fd in s.get("findings", [])],
                     key=lambda r: -r[0])
    total, base = (inv.total if inv else None), ctx.get("base")
    tds = round(base * v["tds_rate"], 2) if (v and base and v.get("tds_rate")) else 0
    P = ctx["policy"]
    approver = None if not total else "AP Manager" if total <= P.ap_manager_limit else "Finance Controller" if total <= P.controller_limit else "CFO"
    blockers = [r[2] for r in reasons if r[0] > 0]
    name = v["name"] if v else (inv.vendor_name if inv else "vendor")
    if decision == "APPROVE":
        action = f"Post to ERP and schedule payment of {inr((total or 0) - tds)} to the master bank account (…{v['bank_account'][-4:]}). Sign-off: {approver}."
        note = (f"Invoice {inv.invoice_number} from {name} for {inr(total)} matched {ctx.get('po')} and passed all checks. " + (f"TDS u/s {v['tds_section']}: {inr(tds)}. " if v.get("tds_section") else "No TDS applies. ") + f"Net payable {inr(total - tds)}. Requires {approver} approval per matrix.")
    elif ctx.get("high_risk"):
        action = f"DO NOT PAY. Call {v['contact']} using the number on file, never the one on the invoice, to verify the bank change. Alert finance controls."
        note = (f"INTERNAL SECURITY ALERT. Do not reply to the sender of this invoice.\n\nInvoice {inv.invoice_number} claiming to be from {name} "
                f"({inr(total)}) asks for payment to a bank account that does not match our vendor master. Payment is blocked. "
                f"Verify by phone with {v['contact']} before any change to vendor bank details.")
    elif decision == "HOLD":
        action = "Route to AP reviewer. Send the drafted query to the vendor if the issue is on their side."
        note = (f"Dear {name},\n\nWe could not process invoice {inv.invoice_number if inv else ''} ({inr(total)}) for the following reason(s):\n"
                + "\n".join(f"• {b}" for b in blockers) + "\n\nPlease send a corrected invoice or clarification and we'll process it promptly.\n\nAccounts Payable, Zephyr Retail")
    else:
        action = "Do not pay. Return the invoice to the vendor with the reason. No further action needed."
        note = (f"Dear {name},\n\nInvoice {inv.invoice_number if inv else ''} ({inr(total)}) has been rejected:\n"
                + "\n".join(f"• {b}" for b in blockers) + "\n\nIf you believe this is an error, please reply with supporting documents.\n\nAccounts Payable, Zephyr Retail")
    return {"decision": decision, "reasons": [{"severity": ["note", "hold", "reject"][r[0]], "stage": r[1], "text": r[2]} for r in reasons],
            "next_action": action, "note": note, "high_risk": bool(ctx.get("high_risk")),
            "payment": {"total": total, "taxable_value": base, "tds_section": v.get("tds_section") if v else None,
                        "tds": tds, "net_payable": (total - tds) if total else None, "approver": approver}}


# ---------------------------------------------------------------- runner
def _exec(fn, name, ctx):
    ts = time.time()
    try:
        st, stop = fn(ctx)
    except Exception as e:  # a stage must never crash the run
        st, stop = result(name, [("hold", f"Internal error in {name}: {e}")], {}, ""), True
    st["ms"] = round((time.time() - ts) * 1000)
    return st, stop


def _skipped(name):
    return {"stage": name, "status": "skipped", "severity": "ok", "summary": "Skipped: an earlier stage could not complete.",
            "findings": [], "evidence": {}, "ms": 0}


def _save(ctx, stages, dec, duration):
    inv, v, usage = ctx.get("inv"), ctx.get("vendor"), ctx.get("usage", {})
    store.finish(ctx["run_id"], decision=dec["decision"], vendor_id=v["id"] if v else None,
                 vendor_name=(v["name"] if v else (inv.vendor_name if inv else None)),
                 invoice_number=inv.invoice_number if inv else None, inv_key=norm_inv(inv.invoice_number or "") if inv else None,
                 fy=ctx.get("fy"), po_number=ctx.get("po"), base=ctx.get("base"), total=inv.total if inv else None,
                 high_risk=int(dec["high_risk"]), duration_s=duration, cost_usd=usage.get("cost_usd", 0),
                 cached=int(bool(usage.get("cached"))),
                 data={"stages": stages, "decision": dec, "extracted": inv.model_dump() if inv else None,
                       "digital": ctx.get("digital"), "usage": usage})


def run(pdf: bytes, filename: str, policy: store.Policy | None = None, cached_only: bool = False, pace: float | None = None):
    """Generator: yields ('start', {...}), ('stage', dict) for 'running' and final state of each stage, then ('done', run).
    Stages 1-4 stream as they execute. Stages 5-7 (PO balance, duplicates, decision) run atomically under DECIDE_LOCK together
    with the DB write, so two concurrent runs can't both consume the same PO balance or both pass a duplicate check;
    their results are then streamed with the same pacing."""
    sha = hashlib.sha256(pdf).hexdigest()
    run_id = store.create(filename, sha)
    vendors, pos, paid = load_master()
    ctx = {"pdf": pdf, "sha": sha, "run_id": run_id, "vendors": vendors, "pos": pos, "paid": paid,
           "policy": policy or store.get_policy(), "cached_only": cached_only}
    pace = PACE if pace is None else pace
    t0, slept, stages, stopped, saved = time.time(), 0.0, [], False, False
    try:
        yield "start", {"run_id": run_id, "stages": STAGES}
        for fn in (s_ingest, s_extract, s_validate, s_vendor):
            name = STAGES[len(stages)]
            if stopped:
                st = _skipped(name)
            else:
                yield "stage", {"stage": name, "status": "running"}
                st, stopped = _exec(fn, name, ctx)
                time.sleep(pace); slept += pace
            stages.append(st)
            yield "stage", st
        with DECIDE_LOCK:
            for fn in (s_po, s_duplicate):
                name = STAGES[len(stages)]
                st, stop2 = (_skipped(name), stopped) if stopped else _exec(fn, name, ctx)
                stopped = stopped or stop2
                stages.append(st)
            dec = decide(ctx, stages)
            first = next((r["text"] for r in dec["reasons"] if r["severity"] != "note"), None)
            stages.append({"stage": "Decide", "status": {"APPROVE": "pass", "HOLD": "warn", "REJECT": "fail"}[dec["decision"]],
                           "severity": "ok", "summary": f"{dec['decision']}: " + (first if first and dec["decision"] != "APPROVE" else dec["next_action"]),
                           "findings": [], "evidence": {}, "ms": 0})
            _save(ctx, stages, dec, round(time.time() - t0 - slept, 2))  # real processing time, excluding UI pacing
            saved = True
        for st in stages[4:]:
            if st["status"] != "skipped":
                yield "stage", {"stage": st["stage"], "status": "running"}
                time.sleep(pace)
            yield "stage", st
        yield "done", store.get(run_id)
    finally:
        if not saved:  # client disconnected or crash mid-run: never leave an undecided row
            dec = {"decision": "HOLD", "reasons": [{"severity": "hold", "stage": "Run", "text": "Run was interrupted before a decision. Re-process the invoice."}],
                   "next_action": "Re-process the invoice.", "note": "", "high_risk": False, "payment": {}}
            _save(ctx, stages, dec, round(time.time() - t0 - slept, 2))
