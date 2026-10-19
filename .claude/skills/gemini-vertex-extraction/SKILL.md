---
name: gemini-vertex-extraction
description: How InvoiceIQ calls Gemini on Vertex AI (GCP) to turn an invoice PDF (digital or scanned) into schema-validated JSON cheaply. Use when touching app/extract.py, the extraction schema, the model choice, or cost tracking.
---

# Gemini on Vertex AI: invoice extraction

## Setup
- Auth: Application Default Credentials. Locally: `gcloud auth application-default login`. On Cloud Run: the service account needs `roles/aiplatform.user`. No API keys in code.
- Env: `GCP_PROJECT` (default: the project from `google.auth.default()`), `GCP_LOCATION` (default `us-central1`), `GEMINI_MODEL` (default `gemini-2.5-flash`).
- SDK: `google-genai` (`from google import genai; from google.genai import types`).

```python
client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
resp = client.models.generate_content(
    model=MODEL,
    contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), PROMPT],
    config=types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json",
        response_schema=Invoice,                                    # pydantic model
        thinking_config=types.ThinkingConfig(thinking_budget=0),    # extraction needs no reasoning; cuts cost + latency
    ),
)
inv: Invoice = resp.parsed
u = resp.usage_metadata  # prompt_token_count, candidates_token_count
```

## Rules
- Send the **PDF bytes directly**. Gemini reads both text-layer and scanned PDFs, so there is no OCR dependency. pypdf is used only to *detect* the text layer (routing label + grounding check).
- Schema is a Pydantic model. Every field is Optional, because missing data is a business signal, not a parse error. The prompt says: "Return null when a field is not printed on the document. Never infer or invent."
- Include `confidence: float` per invoice plus `notes: list[str]` for anything odd the model noticed ("handwritten correction", "bank change notice").
- `tax_inclusive: bool` must be extracted explicitly. It drives validation maths.
- One retry on any exception. After that the stage fails with severity `hold`. Never crash a run.
- Cost per call = `prompt_tokens * 0.30e-6 + output_tokens * 2.50e-6` (USD, Gemini 2.5 Flash list price). Store it on the run.

## Cost guardrails
- One LLM call per invoice. Do not add LLM calls for explanations or decisions; those are templated from deterministic results.
- Cap the PDF at 10 pages / 10 MB at ingest.
- Typical invoice is ~1.5k input + ~400 output tokens, about $0.0015.

## Offline/dev fallback
Every successful live call writes `samples/_cache/<sha256>.json`.
- `INVOICEIQ_MOCK=1`: always read from the cache (no GCP needed). Used by the free rules-only eval.
- Live mode (default): if Gemini fails after the retry and a cache entry exists, use it and set `usage.cached = true`. The UI shows a "cached extraction" badge, so it never pretends to be live.
- Per-field confidence is skipped deliberately: model self-report isn't meaningful. One overall `confidence` gates scanned docs only. Digital docs are gated by the grounding check.
