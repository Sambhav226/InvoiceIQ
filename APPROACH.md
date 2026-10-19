# InvoiceIQ: PS-1 Invoice → Reasoned Decision

> **For the AP head, in three lines.** Every vendor invoice PDF, clean or scanned, gets a decision (APPROVE / HOLD / REJECT) in about 5 seconds, with the reason in plain English. It blocks duplicate payments, PO over-billing, GST errors and bank-change fraud before money moves. Running cost is **about ₹0.15 per invoice**. Your AP team only looks at the HOLD queue.

**Candidate:** Sambhav Jha · **Problem:** PS-1 Finance / AP · **Stack:** Python · FastAPI · Gemini 2.5 Flash on Vertex AI (GCP) · SQLite · vanilla JS · Docker → Cloud Run

---

## 1. Why PS-1

| Factor | PS-1 Invoices | PS-2 Vendor onboarding | PS-3 Outreach |
|---|---|---|---|
| Fit with my resume | Document AI and CV (scanned invoices), LLM evaluation (grounding checks), ETL pipelines, FastAPI + Docker + GCP | Rules engine only | LLM/RAG, but the output is subjective |
| Running cost | **One LLM call per invoice**; matching and decisions are deterministic Python | Low | Paid web search + several LLM calls per prospect |
| Demo | Crisp, verifiable right/wrong decisions | Good | Hard to show "correct" live |

**Cost principle:** the LLM only *reads* the document. Validation, matching, duplicate/fraud checks and the decision are **deterministic Python**: free, instant, and auditable. A full live demo costs **< $0.05**.

---

## 2. How an AP clerk uses it

1. Drop the invoice PDF (later: it arrives from the AP inbox automatically).
2. Watch 7 checks run live. Most invoices come out **APPROVE**, with net payable after TDS and the approver needed for that amount.
3. The **HOLD queue** holds only the invoices that need a human. Each one shows the exact problem and a drafted vendor email. The clerk clicks *Approve* / *Reject* with a note, and that is logged.
4. The AP head opens the **Dashboard**: auto-approval rate, ₹ protected, hours saved, review backlog, cost.

---

## 3. Process map

```
 PDF ─► 1 INGEST ─► 2 EXTRACT ─► 3 VALIDATE ─► 4 VENDOR ─► 5 PO MATCH ─► 6 DUPLICATE ─► 7 DECIDE
       sha256,     Gemini 2.5    maths, GST,    master,     explicit /     hash,          APPROVE/HOLD/REJECT
       text layer  Flash →       required       GSTIN,      implied PO,    no.+FY,        + reasons, TDS,
       detection   strict JSON   fields,        full bank   balance,       PO+amount      net payable,
                                 grounding      a/c match   tolerance                     approver, vendor note
```

Every stage returns `{status: pass|warn|fail, summary, evidence, severity: ok|hold|reject}` and streams live to the UI over SSE. **Decision = worst severity.** No LLM makes the decision.

| # | Stage | Checks | Outcome on failure |
|---|---|---|---|
| 1 | Ingest | SHA-256; pypdf text layer → *digital* or *scanned*; ≤ 10 pages | Not a PDF → HOLD |
| 2 | Extract | Gemini reads the PDF natively (text or image) into schema-constrained JSON. Missing fields stay `null`, never guessed. One retry. If the live call fails, the cached extraction is used and labelled as cached | HOLD |
| 3 | Validate | Each line: qty × price = amount; Σ lines = subtotal; subtotal + tax = total (±₹1). Required fields present; date not in the future. **GST:** GSTIN format, tax shown separately (Rule 46). Vendor state ≠ buyer state → IGST; same state → CGST + SGST. **Grounding (digital PDFs):** invoice no., GSTIN and total must appear in the PDF text after digit normalisation (hallucination guard). **Scanned:** model confidence must be ≥ 0.85 | HOLD |
| 4 | Vendor | Match on GSTIN first, then fuzzy name (≥ 0.85). Full bank account + IFSC vs master. Payment always goes to the master account, never the one printed on the invoice | Blocked → REJECT · unknown → HOLD (route to onboarding) · **bank differs → HOLD, HIGH RISK** |
| 5 | PO match | Explicit PO, or *implied* (unique open PO of this vendor whose remaining balance fits). Ownership, open status, and **cumulative balance**: invoice base ≤ remaining (PO − billed − approved − pending holds) + tolerance min(2% of invoice, ₹1,000) | Over balance / closed / other vendor's / ambiguous → HOLD |
| 6 | Duplicate | Same file hash; same vendor + normalised no. + financial year (`INV-0042` ≡ `inv 42`, FY-aware so yearly number resets aren't flagged); **same vendor + same PO + same total** as a paid or approved invoice (rebill under a new number) | Confirmed → REJECT |
| 7 | Decide | Policy → decision + reasons, TDS (194C/194J) and net payable, approver from the approval matrix, drafted vendor/AP note | — |

**Approval matrix:** ≤ ₹1,00,000 → AP Manager · ≤ ₹10,00,000 → Finance Controller · above → CFO.
**TDS:** from the vendor master (194C contractors 2%, 194J technical services 2%), on the taxable value.

---

## 4. Edge cases (all real AP failure modes)

| # | Scenario | Why it matters | Expected |
|---|---|---|---|
| **H1** | Clean digital invoice, explicit PO, itemised, CGST + SGST | Happy path | **APPROVE**, net payable, approver = AP Manager |
| **E1** | **Phone-scanned, skewed invoice with one bundled line "incl. 18% GST"**, no tax breakup | Small vendors send scans. A tax-inclusive invoice without a breakup isn't a valid GST tax invoice, so the input tax credit is at risk | Vision extraction works and the base is back-computed, so the PO amount matches. Still **HOLD: "request a proper tax invoice, ITC at risk"** |
| **E2** | **Partial-billing overrun.** PO ₹5,00,000, ₹3,00,000 already billed across 2 earlier invoices; new invoice ₹2,40,000 + IGST | Milestone billing. Each invoice looks fine on its own; only the running total is wrong | **HOLD:** "exceeds remaining PO balance of ₹2,00,000 by ₹40,000" |
| **E3** | **Rebill under a new number.** A paid invoice (INV-0042, ₹23,600, PO-0150) resubmitted as `ACM/26-27/118` with a new date and file | The file hash and invoice number both differ, so the simple checks miss it | **REJECT:** "same PO + same amount as INV-0042, paid 18-Sep-2026" |
| **E4** | **Bank-change fraud (BEC).** Known vendor (name variant), **no PO reference**, footer: "Please note our new bank account…" | Bank-change scams are the most common AP fraud vector | Fuzzy vendor match passes and the implied PO is found uniquely, but **bank ≠ master → HOLD, HIGH RISK, payment blocked**. Action: call back on the contact number already on file |
| **E5** | **The "tired Friday" invoice.** 2 pages, 12 line items; one line is 120 × ₹145 printed as ₹17,500, and IGST is charged on a Karnataka → Karnataka supply. Subtotal and total are internally consistent | A clerk who checks only the totals approves it. Line-level maths and place-of-supply GST logic are what catch it | **HOLD:** "Line 7: … ₹100 over-billed" + "IGST on an intra-state supply, should be CGST + SGST". PO, TDS 194J and the Finance Controller sign-off are still worked out |

Video: H1 → E4 (the payoff) → Dashboard. Live interview: all six, then any uploaded invoice.

---

## 5. Assumptions

1. INR, Indian GST; buyer is *Zephyr Retail Pvt Ltd*, Karnataka (GSTIN state 29).
2. 2-way match (invoice ↔ PO). Fine for service POs. Goods POs need a GRN for 3-way matching, which is the first thing to add next.
3. One GST rate per invoice. HSN-level rate checks are a follow-up.
4. The procurement system is a JSON/CSV stub: vendor master, PO book, paid ledger. In production this would be ERP APIs.
5. Nothing auto-pays. The output is a decision plus a posting-ready record.

---

## 6. JD → where it shows up

| JD asks | In this build |
|---|---|
| Business process → workflow spec | §3 process map + policy, built with project skills in `.claude/skills/` |
| AI tooling to generate automation | Built with Claude Code. Live: change a rule in plain English → `eval.py` green → re-run |
| Design → testing → rollout → monitoring | `eval.py` regression suite; Docker → Cloud Run; dashboard monitoring strip |
| KPIs / ROI to non-technical audiences | Dashboard tiles with formulas; the three-line summary for the AP head at the top of this doc |
| Customer enablement | §2 clerk workflow, reviewer actions, drafted vendor emails |

---

## 7. Rollout plan and next steps

**Rollout:** 2 weeks in *shadow mode* (decisions logged, humans still process everything), then compare against the clerks' decisions. Turn on auto-approve for vendors whose agreement rate is ≥ 98%.
**Next:** GRN 3-way match · inbox ingestion (Gmail → Pub/Sub) · GSTR-2B filing check · credit notes · invoices spanning several POs · reviewer feedback to tune tolerances · ERP posting.
