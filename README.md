# InvoiceIQ: vendor invoice PDF → APPROVE / HOLD / REJECT, with reasons

Every invoice, clean or phone-scanned, gets a decision in about 5 seconds with the reason in plain English. It catches duplicate payments, PO over-billing, GST errors and bank-change fraud before money moves. **About ₹0.15 (≈$0.002) per invoice.** AP only works the HOLD queue.

> Case study PS-1 (Finance / AP). Design, process map, edge cases and assumptions: **[APPROACH.md](APPROACH.md)**.

```
PDF ─► Ingest ─► Extract (Gemini 2.5 Flash) ─► Validate ─► Vendor ─► PO match ─► Duplicate ─► Decide
        text layer?   schema-locked JSON         maths, GST,  GSTIN,    cumulative   hash, no.+FY,  rules, TDS, approver,
                                                  grounding    bank a/c  balance      PO+amount      drafted vendor note
```
Only *Extract* uses an LLM. Every decision is made by deterministic rules you can audit line by line ([rules](.claude/skills/ap-decision-rules/SKILL.md)).

## Demo scenarios
| | Scenario | Decision |
|---|---|---|
| H1 | Clean digital invoice, PO, CGST + SGST | ✅ APPROVE, net payable + approver |
| E1 | Phone-scanned, skewed; "inclusive of GST" with no breakup | ⏸ HOLD: not a valid tax invoice, ITC at risk |
| E2 | Third milestone invoice that over-bills a ₹5L PO | ⏸ HOLD: exceeds remaining balance by ₹40,000 |
| E3 | Already-paid invoice re-billed under a new number | ✕ REJECT: same PO + amount as INV-0042 |
| E4 | Known vendor, no PO, "please pay our new bank account" | ⏸ HOLD, **HIGH RISK**: payment blocked, call back on the number on file |
| E5 | 2 pages, 12 lines; totals add up, but one line is ₹100 off and IGST is charged within Karnataka | ⏸ HOLD: both hidden errors named |

## Beyond the pipeline
- **Rules tab:** the AP policy (tolerances, sign-off limits, confidence floors, inferred-PO behaviour) is editable in plain English. **Preview impact** replays every scenario under the current and the proposed rules and shows which decisions flip, before anything is saved. No code, no redeploy.
- **ROI calculator** on the dashboard: enter your invoice volume and clerk cost to get hours freed, labour saved, overpayments caught and net benefit per year. It uses the hold rate actually observed in your runs.
- **Reviewer loop:** HOLDs are approved or rejected with a note, and the override rate shows when rules are too strict.

## Accounts and data retention
Sign in with **Google**, **email and password**, or **Try as guest**. Every run, source PDF and rule set is private to that user (Firestore `users/{uid}/…`).
- Each record carries `expire_at`; Firestore TTL policies delete it automatically. Reads hide expired data even before the sweep.
- Users choose **7, 30 or 90 days** (default 30) in *Data & privacy*. A change applies to data already stored. **Delete all my data now** wipes runs, PDFs and rules.
- Guests: data is deleted after 24 hours. Signing in links the guest session, so its invoices carry over.
- Local dev (no `INVOICEIQ_AUTH`): a single local user on SQLite, no sign-in.

One-time setup (project `jobpilot-66`):
1. [Firebase console](https://console.firebase.google.com) → Add project → pick the existing Google Cloud project.
2. Authentication → Sign-in method → enable **Google**, **Email/Password**, **Anonymous**. Under Settings → Authorized domains, add the Cloud Run host.
3. Project settings → Your apps → Web app → copy `apiKey`, `authDomain`, `appId`.
4. Firestore and its TTL policies:
   ```bash
   gcloud firestore databases create --location=nam5 --project jobpilot-66
   for g in runs files users; do gcloud firestore fields ttls update expire_at --collection-group=$g --enable-ttl --async --project jobpilot-66; done
   ```
5. Deploy with `INVOICEIQ_STORE=firestore,INVOICEIQ_AUTH=firebase,FIREBASE_API_KEY=…,FIREBASE_AUTH_DOMAIN=…,FIREBASE_APP_ID=…` added to `--set-env-vars`.

## Run locally
```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
gcloud auth application-default login            # Vertex AI via your GCP project
export GCP_PROJECT=<your-project>                # needs Vertex AI API enabled
.venv/bin/uvicorn app.main:app --reload          # http://localhost:8000
```
- `python eval.py` runs the regression suite: all 5 scenarios must hit their expected decision (live, ~$0.01).
- `INVOICEIQ_MOCK=1 python eval.py` checks the rules only, free, from cached extractions (written by the first live run).
- `python test_rules.py` runs offline rule regressions: number normalisation, date formats, credit notes, re-bills, concurrent duplicates.
- `python scripts/make_samples.py` regenerates the sample PDFs.

## Deploy (Cloud Run)
```bash
gcloud run deploy invoiceiq --source . --region us-central1 --allow-unauthenticated \
  --set-env-vars GCP_PROJECT=$GCP_PROJECT,GCP_LOCATION=us-central1
```
The Cloud Run service account needs `roles/aiplatform.user`. No API keys are stored anywhere.

## Layout
```
app/extract.py    Gemini on Vertex → validated Invoice model (+ cost, cache fallback)
app/pipeline.py   7 stages + decision policy (deterministic)
app/store.py      SQLite run history, KPIs
app/main.py       FastAPI: upload, SSE live stream, history, review, metrics
static/index.html Live run view + dashboard (vanilla JS, no build)
data/             Procurement stub: vendor master, PO book, paid ledger
samples/          Demo invoices + manifest (expected decisions)
eval.py           Regression suite
.claude/skills/   Project skills used to build this with Claude Code
```
