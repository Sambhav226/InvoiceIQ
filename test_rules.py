"""Rule regression checks (no network, no cost): python test_rules.py
Each block pins a bug found in the multi-agent code review."""
import os
import tempfile
import threading

os.environ["INVOICEIQ_DB"] = tempfile.mktemp(suffix=".db")
os.environ["INVOICEIQ_PACE"] = "0"

from app import pipeline, store  # noqa: E402
from app.extract import Invoice  # noqa: E402

PDF = b"%PDF-1.4 fake"
ACME = dict(vendor_name="Acme Office Supplies Pvt Ltd", vendor_gstin="29AABCA1234F1Z5", bank_account="50100234564521",
            ifsc="HDFC0001234", confidence=0.95, tax_rate=0.18)


def go(n=0, policy=None, **fields):
    """Run the pipeline with a fake extraction. Each call gets a distinct file hash via n."""
    inv = Invoice(**{**ACME, **fields})
    pipeline.extract = lambda pdf, sha, **_: (inv, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0, "model": "fake", "cached": False})
    pipeline.s_ingest = lambda ctx: (ctx.update(text="", digital=False) or pipeline.result("Ingest", [], {}, "ok"), False)
    out = [p for k, p in pipeline.run(PDF + str(n).encode(), "t.pdf", policy=policy) if k == "done"][0]
    return out["decision"], " ".join(r["text"] for r in out["data"]["decision"]["reasons"])


def inv(no, total, po="PO-2026-0412", date="2026-10-01"):
    base = round(total / 1.18, 2)
    return dict(invoice_number=no, invoice_date=date, po_reference=po, subtotal=base, cgst=round(base * .09, 2),
                sgst=round(total - base - round(base * .09, 2), 2), total=total)


# normalisation keeps significant zeros
assert pipeline.norm_inv("INV-0042") == pipeline.norm_inv("inv 42") == "INV42"
assert pipeline.norm_inv("INV-100") != pipeline.norm_inv("INV-10")
# date formats
for s in ("2026-08-28", "28-Aug-2026", "28 Aug 2026", "28/08/2026", "28.08.2026", "2026-08-28T00:00:00"):
    assert str(pipeline.parse_date(s)) == "2026-08-28", s
# nulls from the model don't kill extraction
assert Invoice.model_validate({"line_items": None, "notes": None, "confidence": None}).line_items == []

store.reset()
assert go(1, **inv("INV-10", 5900))[0] == "APPROVE"
assert go(2, **inv("INV-100", 11800))[0] == "APPROVE", "INV-100 must not collide with INV-10"
d, why = go(3, **inv("INV-101", 5900))
assert d == "REJECT" and "already-approved" in why, "re-bill vs approved run"
d, why = go(4, **inv("INV-0042", 23600, po="PO-2026-0150", date="28 Aug 2026"))
assert d == "REJECT" and "already paid" in why, "paid duplicate with non-ISO date"
d, why = go(5, **inv("CN-1", -11800))
assert d == "HOLD" and "Credit note" in why
# percent tax rate on tax-inclusive invoice: base must be total/1.18, not total/19
d, _ = go(6, invoice_number="BL-9", invoice_date="2026-10-01", po_reference="PO-2026-0412", tax_inclusive=True, tax_rate=18, total=11800)
assert abs(store.listing()[0]["base"] - 10000) < 1
# corrected resend after a reviewer-rejected HOLD is not a duplicate
store.reset()
d, _ = go(7, **{**inv("INV-700", 5900), "vendor_gstin": None})
assert d == "HOLD"
store.review(store.listing()[0]["id"], "reject", "no GSTIN")
assert go(8, **inv("INV-700", 5900))[0] == "APPROVE"
# concurrent identical invoices: exactly one approved
store.reset()
res = []
ts = [threading.Thread(target=lambda i=i: res.append(go(0, **inv("INV-900", 5900))[0])) for i in range(2)]
for t in ts: t.start()
for t in ts: t.join()
assert sorted(res) == ["APPROVE", "REJECT"], res
# tunable policy: an inferred PO approves by default, and holds when the policy demands a PO reference
store.reset()
chairs = {**inv("INV-3000", 159300), "po_reference": None}
assert go(30, **chairs)[0] == "APPROVE"
store.reset()
d, why = go(31, policy=store.Policy(approve_inferred_po=False), **chairs)
assert d == "HOLD" and "Policy requires a PO reference" in why
try:
    store.Policy(ap_manager_limit=5e6, controller_limit=1e6)
    raise AssertionError("inverted sign-off limits must be rejected")
except ValueError:
    pass
print("all rule checks passed")
