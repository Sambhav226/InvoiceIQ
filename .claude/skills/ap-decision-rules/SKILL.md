---
name: ap-decision-rules
description: Domain rules for the InvoiceIQ accounts-payable pipeline: stage contract, validation maths, GST, PO matching, duplicate detection, fraud checks, TDS, approval matrix and the APPROVE/HOLD/REJECT policy. Use when writing or changing anything in app/pipeline.py or the procurement data in data/.
---

# AP decision rules (InvoiceIQ)

Source of truth for business logic. APPROACH.md explains the *why*; this file is the *how*.

## Stage contract
Every stage returns `{"stage", "status": "pass"|"warn"|"fail", "summary": str, "evidence": dict, "severity": "ok"|"hold"|"reject"}`.
- `summary` is ONE plain-English sentence a non-technical AP clerk understands. Use real numbers, formatted as ₹1,23,456.
- `evidence` holds the raw values compared, so the UI can show "expected vs found".
- Stages never raise. Catch the error and return `fail` with `severity: hold`.
- A stage can report several findings. `summary` joins them, and severity is the worst of them.

## Decision = worst severity
`reject` > `hold` > `ok`. Reasons = every non-pass finding, ordered by severity. No LLM in the decision path.

## Tunable policy
Thresholds an ops team owns live in `store.Policy` (bounds-validated) and are edited on the Rules tab: PO tolerance % and ₹ cap, scan-confidence floor, vendor-name match, the AP Manager and Finance Controller sign-off limits, and whether an inferred PO may auto-approve. Stages read them from `ctx["policy"]`. Never hardcode these in a stage. Values below are the defaults. Fraud and duplicate controls are deliberately NOT tunable.

## Numbers
- Money as `float` rounded to 2 dp. Compare with `abs(a-b) <= 1.0` (₹1 rounding tolerance).
- GST-inclusive invoices (`tax_inclusive: true`): base = total / (1 + rate). Match the PO on the base, but the invoice is HOLD (Rule 46: tax not shown separately, so ITC is at risk).
- PO tolerance: `min(2% of invoice base, ₹1,000)`, applied to the invoice, not to the balance.
- Remaining balance = `po.amount - po.billed_to_date - Σ base of other runs on this PO with decision APPROVE or unreviewed HOLD` (holds reserve balance; a reviewer reject releases it).
- TDS on base, from the vendor master `tds_section`/`tds_rate`. Net payable = total - TDS.
- Approval matrix on total: ≤ ₹1L AP Manager, ≤ ₹10L Finance Controller, else CFO.

## Normalisation
- Invoice number: uppercase, keep only `[A-Z0-9]`, strip leading zeros from digit runs. `INV-0042`, `inv 42` and `INV0042` all become `INV42`. Duplicate key = vendor + normalised no. + Indian FY (Apr–Mar) of the invoice date.
- Vendor name: lowercase, drop punctuation and suffixes (`pvt`, `ltd`, `private`, `limited`, `llp`, `inc`, `co`), collapse spaces. Fuzzy match with `difflib.SequenceMatcher` ≥ 0.85.
- GSTIN match beats name match. GSTIN format: `^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$`. The first 2 digits are the state code. Buyer state = 29 (Karnataka).
- Bank: compare the FULL account number (digits only) + IFSC. Pay only the master account.
- Grounding (digital PDFs only): strip all non-alphanumerics from both the PDF text and the value, then substring-check invoice no., GSTIN, and total (both `123456.00` and `123456` forms).

## Policy
| Condition | Severity |
|---|---|
| Vendor `blocked` | reject |
| Duplicate: same hash, or vendor+no.+FY, or vendor+PO+total vs paid/approved | reject |
| Vendor unknown (route to onboarding) | hold |
| PO belongs to another vendor, closed, or not found | hold |
| Bank account differs from master | hold (HIGH RISK) |
| Over remaining PO balance beyond tolerance | hold |
| Missing invoice no / date / total / vendor | hold |
| Arithmetic mismatch, wrong GST type for the states, invalid GSTIN, tax-inclusive (no breakup), future date | hold |
| Ungrounded value (digital) or confidence < 0.85 (scanned) | hold |
| Implied PO, exactly one candidate | warn (ok) |
| Implied PO, 0 or >1 candidates | hold |

## Adding a rule
1. Add a row to the policy table above.
2. Implement it inside the relevant stage function (don't add a new stage unless the UI needs a new card).
3. Add or adjust a sample in `samples/` and its expected decision in `samples/manifest.json`.
4. Run `python eval.py`. All rows must pass.
