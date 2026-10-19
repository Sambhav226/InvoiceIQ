"""Invoice PDF -> structured fields via Gemini on Vertex AI. The only paid step in the pipeline."""
import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator

PROJECT = os.getenv("GCP_PROJECT")  # None -> taken from gcloud ADC
LOCATION = os.getenv("GCP_LOCATION", "us-central1")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
PRICE_IN, PRICE_OUT = 0.30e-6, 2.50e-6  # USD per token, Gemini 2.5 Flash list price
CACHE = Path(__file__).parent.parent / "samples" / "_cache"


class LineItem(BaseModel):
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None


class Invoice(BaseModel):
    vendor_name: Optional[str] = None
    vendor_gstin: Optional[str] = None
    bill_to_gstin: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None  # ISO YYYY-MM-DD
    due_date: Optional[str] = None
    po_reference: Optional[str] = None
    currency: Optional[str] = None
    line_items: list[LineItem] = []
    subtotal: Optional[float] = None
    cgst: Optional[float] = None
    sgst: Optional[float] = None
    igst: Optional[float] = None
    total_tax: Optional[float] = None
    tax_inclusive: bool = False
    tax_rate: Optional[float] = None  # e.g. 0.18
    total: Optional[float] = None
    bank_account: Optional[str] = None
    ifsc: Optional[str] = None
    confidence: float = 0.0
    notes: list[str] = []

    @field_validator("line_items", "notes", mode="before")
    @classmethod
    def _none_list(cls, v):  # model sometimes returns null for empty lists
        return v or []

    @field_validator("confidence", "tax_inclusive", mode="before")
    @classmethod
    def _none_scalar(cls, v, info):
        return v if v is not None else (0.0 if info.field_name == "confidence" else False)


PROMPT = """You are an accounts-payable data extractor. Read this vendor invoice and return the fields in the schema.
Rules:
- Copy values exactly as printed. Return null when a field is not printed on the document. Never infer or invent a value.
- invoice_date / due_date as YYYY-MM-DD.
- Amounts as plain numbers (no currency symbols or commas).
- po_reference: only if a purchase order number is printed; otherwise null.
- tax_inclusive = true when line prices/total are stated as inclusive of GST/tax; then subtotal may be null.
- tax_rate as a fraction (18% -> 0.18).
- bank_account: digits only, as printed in the payment details.
- confidence: 0-1, your confidence that every extracted value is correct (lower it for blurry scans or ambiguity).
- notes: short observations an AP clerk should know (e.g. 'bank details change notice', 'handwritten correction', 'stamp obscures total')."""

_client = None


def extract(pdf_bytes: bytes, sha256: str, cached_only: bool = False) -> tuple[Invoice, dict]:
    """Returns (invoice, usage). usage = {input_tokens, output_tokens, cost_usd, model, cached: False|"mock"|"fallback"}."""
    cache_file = CACHE / f"{sha256}.json"
    if cached_only or os.getenv("INVOICEIQ_MOCK") == "1":  # what-if previews never spend tokens
        return _from_cache(cache_file, "mock")
    try:
        inv, usage = _call_gemini(pdf_bytes)
    except Exception:
        # Demo resilience: a previously-seen file falls back to its last good extraction, labelled as cached.
        if cache_file.exists():
            return _from_cache(cache_file, "fallback")
        raise
    if os.getenv("INVOICEIQ_AUTH") != "firebase":  # server: user data lives only in their run (with its expiry), never here
        CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(inv.model_dump_json(indent=2))
    return inv, usage


def _from_cache(cache_file: Path, why: str) -> tuple[Invoice, dict]:
    if not cache_file.exists():
        raise RuntimeError("no cached extraction for this file (mock mode)")
    return Invoice.model_validate_json(cache_file.read_text()), {
        "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "model": MODEL, "cached": why}


def _call_gemini(pdf_bytes: bytes) -> tuple[Invoice, dict]:
    from google import genai
    from google.genai import types

    global _client
    if _client is None:
        project = PROJECT
        if not project:
            import google.auth
            project = google.auth.default()[1]
        _client = genai.Client(vertexai=True, project=project, location=LOCATION)
    last = None
    for _ in range(2):  # one retry
        try:
            resp = _client.models.generate_content(
                model=MODEL,
                contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), PROMPT],
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=Invoice,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            inv = resp.parsed if isinstance(resp.parsed, Invoice) else Invoice.model_validate(json.loads(resp.text))
            break
        except Exception as e:
            last = e
    else:
        raise last
    u = resp.usage_metadata
    tin, tout = u.prompt_token_count or 0, u.candidates_token_count or 0
    return inv, {"input_tokens": tin, "output_tokens": tout,
                 "cost_usd": round(tin * PRICE_IN + tout * PRICE_OUT, 6), "model": MODEL, "cached": False}
