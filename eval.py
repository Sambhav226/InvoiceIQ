"""Regression eval: every sample in samples/manifest.json must produce its expected decision and reason.
Live (default) calls Gemini (~$0.01 total). INVOICEIQ_MOCK=1 re-checks the rules for free from cached extractions.
Usage: python eval.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["INVOICEIQ_DB"] = tempfile.mktemp(suffix=".db")  # isolated history: demo DB untouched, PO balances fresh
os.environ["INVOICEIQ_PACE"] = "0"

from app import pipeline  # noqa: E402  (env must be set first)

SAMPLES = Path(__file__).parent / "samples"
manifest = json.loads((SAMPLES / "manifest.json").read_text())
fails, cost = 0, 0.0
print(f"{'sample':32} {'expected':9} {'got':9} {'sec':>5} {'$':>8}  result")
for m in manifest:
    run = None
    for kind, payload in pipeline.run((SAMPLES / m["file"]).read_bytes(), m["file"]):
        if kind == "done":
            run = payload
    dec = run["data"]["decision"]
    reasons = " ".join(r["text"] for r in dec["reasons"])
    ok = dec["decision"] == m["expected"] and m["reason"].lower() in reasons.lower()
    fails += not ok
    cost += run["cost_usd"] or 0
    print(f"{m['file']:32} {m['expected']:9} {dec['decision']:9} {run['duration_s']:5.1f} {run['cost_usd'] or 0:8.5f}  "
          f"{'PASS' if ok else 'FAIL'}{'' if ok else '  ← ' + reasons[:160]}")
print(f"\n{len(manifest) - fails}/{len(manifest)} passed · total LLM cost ${cost:.4f}")
sys.exit(1 if fails else 0)
