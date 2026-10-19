---
name: live-run-ui
description: UI contract for InvoiceIQ's single-page frontend (static/index.html): the live run view streamed over SSE and the history/KPI dashboard. Use when building or changing the frontend or the API endpoints it consumes.
---

# InvoiceIQ UI

One file: `static/index.html` (vanilla JS + CSS, no build step, no framework). Served by FastAPI at `/`.

## API it consumes
| Method | Path | Returns |
|---|---|---|
| GET | `/api/samples` | `[{file, title, scenario, expected, reason}]` |
| GET | `/api/samples/{file}` | the sample PDF |
| POST | `/api/upload` (multipart `file`) or `/api/upload/sample/{file}` | `{token}` (single-use) |
| GET | `/api/process/{token}` | SSE: `event: start`, `event: stage` (stage dict, sent once as `running` and once final), `event: done` (full run) |
| GET | `/api/runs` | run summaries newest first, each with its top `reason` |
| GET | `/api/runs/{id}/pdf` | the processed PDF (for replay) |
| GET | `/api/runs/{id}` | full run incl. stages, extracted, decision |
| GET | `/api/metrics` | KPI numbers + formulas |
| POST | `/api/runs/{id}/review` (`{action: approve|reject, note}`) | updated run; only for HOLD |
| POST | `/api/reset` | clears run history (demo); policy is kept |
| GET | `/api/policy` | `{policy, defaults}` |
| PUT | `/api/policy` (`store.Policy` JSON) | saved policy; 422 on out-of-range values |
| POST | `/api/policy/preview` (`store.Policy` JSON) | per scenario `{current, proposed, changed}`: replayed in a scratch DB from cached extractions, so it costs nothing |

Tabs: **Process**, **Dashboard** (KPI bento, ROI calculator, history), **Rules** (policy editor + what-if preview).

## Layout
- Top bar: product name, two tabs: **Process** and **Dashboard**.
- **Process tab:** left column has the drop-zone plus sample cards (title, scenario one-liner, "expected: HOLD" chip). Right column has a vertical timeline of the 7 stage cards. Each card shows icon, name, status pill, summary sentence, and a collapsible evidence table. Below the timeline sit the decision banner (green APPROVE / amber HOLD / red REJECT), the reasons list, the recommended next action, the drafted note with a copy button, and the extracted fields in a collapsible block.
- HOLD decisions show a reviewer panel: note field plus Approve / Reject buttons. The result is logged on the run (`review`).
- Badges: `scanned`/`digital`, `cached extraction` when the LLM fell back to cache, `HIGH RISK` for bank mismatch.
- **Dashboard tab:** KPI tiles row (each with an ⓘ tooltip showing its formula), a review-queue count, then a history table. A row click opens that run in the Process tab (replays the trace from stored stages).

## Visual rules
- Light, calm, finance-grade: neutral background, one accent colour, status colours only for status.
- System font stack; tabular numbers (`font-variant-numeric: tabular-nums`) for money.
- Stage card states: pending (grey, dashed), running (accent pulse), pass (green check), warn (amber), fail (red).
- Money formatted `en-IN` with ₹. Durations in seconds to 1 dp. Cost in USD to 4 dp.
- Works at 1280px (demo screen) and stays readable at 390px. Visible keyboard focus. Status is never shown by colour alone (always paired with an icon + word).
- No external JS libraries.
