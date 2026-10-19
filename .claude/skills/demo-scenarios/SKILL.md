---
name: demo-scenarios
description: How InvoiceIQ's test invoices, procurement stub data and the eval harness fit together, and how to add a new edge case. Use when generating sample PDFs (scripts/make_samples.py), editing data/*.json|csv, or running/extending eval.py.
---

# Demo scenarios and eval

## Files
- `data/vendors.json`: vendor master (id, name, gstin, bank_account, ifsc, status, contact, tds_section, tds_rate).
- `data/purchase_orders.csv`: po_number, vendor_id, description, amount (ex-tax), tax_rate, billed_to_date, status.
- `data/paid_invoices.csv`: ledger of already-paid invoices (vendor_id, invoice_number, po_number, total, invoice_date, paid_on).
- `scripts/make_samples.py`: generates every `samples/*.pdf` deterministically (reportlab for digital, PIL rasterise + rotate + noise for scanned) and writes `samples/manifest.json` (file, title, scenario, expected, reason).
- `eval.py`: runs each manifest entry through the real pipeline against a temp DB and asserts `decision == expected` and that `reason` appears in the reasons. Prints a table and exits non-zero on any miss.

## Scenario set (keep in sync with APPROACH.md §4)
| file | expected | reason substring |
|---|---|---|
| h1_acme_clean.pdf | APPROVE | — |
| e1_brightline_scanned.pdf | HOLD | tax invoice |
| e2_northwind_overrun.pdf | HOLD | remaining PO balance |
| e3_acme_rebill.pdf | REJECT | INV-0042 |
| e4_acme_bank_change.pdf | HOLD | bank details |
| e5_brightline_multipage.pdf | HOLD | IGST (also flags line 7: ₹100 over-billed) |

Every generator must be byte-deterministic: the sha256 of each PDF is the extraction-cache key. PIL PDFs pin `creationDate`/`modDate`; reportlab uses `invariant=1`.

## Rules
- Samples must look like real invoices: letterhead, GSTIN, address blocks, line-item table, tax breakup, bank details, terms. Each vendor gets its own layout so extraction isn't trivially templated.
- Eval uses its own temp DB (`INVOICEIQ_DB`). The UI has a **Reset demo** button (`POST /api/reset`) that clears runs, because re-running H1 is correctly flagged as a duplicate.
- Live eval costs about $0.01 for 5 invoices. Run it before every demo. `INVOICEIQ_MOCK=1 python eval.py` re-checks the rules for free using cached extractions.

## Adding an edge case
1. Add the vendor/PO/ledger rows it needs in `data/`.
2. Add a generator function + manifest entry in `make_samples.py`. Run it.
3. Add the row to the table above and to APPROACH.md §4.
4. `python eval.py` must be green.
