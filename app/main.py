"""InvoiceIQ API + UI. Run: uvicorn app.main:app --reload"""
import hashlib
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import pipeline, store

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
UPLOADS = ROOT / "uploads"  # every processed PDF, by sha256, so any run can show its source
MAX_BYTES = 10 * 1024 * 1024
app = FastAPI(title="InvoiceIQ")
pending: dict[str, tuple[bytes, str]] = {}  # upload token -> (pdf, filename); consumed by the stream


def _stash(pdf: bytes, name: str) -> dict:
    while len(pending) >= 20:  # unclaimed uploads: drop the oldest so memory stays bounded
        pending.pop(next(iter(pending)))
    token = uuid.uuid4().hex
    pending[token] = (pdf, name)
    return {"token": token}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/samples")
def samples():
    return json.loads((SAMPLES / "manifest.json").read_text())


@app.get("/api/samples/{name}")
def sample_pdf(name: str):
    path = (SAMPLES / name).resolve()
    if path.parent != SAMPLES or path.suffix != ".pdf" or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf")


@app.post("/api/upload")
async def upload(file: UploadFile):
    pdf = await file.read(MAX_BYTES + 1)
    if len(pdf) > MAX_BYTES:
        raise HTTPException(413, "PDF larger than 10 MB")
    return _stash(pdf, file.filename or "upload.pdf")


@app.post("/api/upload/sample/{name}")
def upload_sample(name: str):
    path = (SAMPLES / name).resolve()
    if path.parent != SAMPLES or not path.exists():
        raise HTTPException(404)
    return _stash(path.read_bytes(), name)


@app.get("/api/process/{token}")
def process(token: str):
    """Runs the pipeline and streams each stage as Server-Sent Events."""
    if token not in pending:
        raise HTTPException(404, "unknown or already-used upload token")
    pdf, name = pending.pop(token)
    UPLOADS.mkdir(exist_ok=True)
    (UPLOADS / f"{hashlib.sha256(pdf).hexdigest()}.pdf").write_bytes(pdf)

    def events():
        for kind, payload in pipeline.run(pdf, name):
            yield f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/runs")
def runs():
    return store.listing()


@app.get("/api/runs/{run_id}")
def run(run_id: int):
    r = store.get(run_id)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/runs/{run_id}/pdf")
def run_pdf(run_id: int):
    r = store.get(run_id)
    path = UPLOADS / f"{r['sha256']}.pdf" if r else None
    if not path or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf")


class Review(BaseModel):
    action: str
    note: str = ""


@app.post("/api/runs/{run_id}/review")
def review(run_id: int, body: Review):
    r = store.get(run_id)
    if not r or r["decision"] != "HOLD":
        raise HTTPException(400, "only HOLD runs can be reviewed")
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    return store.review(run_id, body.action, body.note[:500])


@app.get("/api/metrics")
def metrics():
    return store.metrics()


@app.get("/api/policy")
def get_policy():
    return {"policy": store.get_policy(), "defaults": store.Policy()}


@app.put("/api/policy")
def put_policy(p: store.Policy):
    return store.set_policy(p)


@app.post("/api/policy/preview")
def preview_policy(p: store.Policy):
    """What-if: replay every demo scenario under the saved rules and under `p`, each in a throwaway history,
    from cached extractions (no model calls). Returns which decisions would change."""
    def replay(policy):
        out = {}
        with store.scratch_db():
            for m in samples():
                run = next(r for k, r in pipeline.run((SAMPLES / m["file"]).read_bytes(), m["file"], policy=policy,
                                                      cached_only=True, pace=0) if k == "done")
                d = run["data"]["decision"]
                out[m["file"]] = {"decision": d["decision"], "approver": d["payment"].get("approver"),
                                  "reason": next((r["text"] for r in d["reasons"] if r["severity"] != "note"), "")}
        return out
    now, new = replay(store.get_policy()), replay(p)
    return [{"file": m["file"], "title": m["title"], "current": now[m["file"]], "proposed": new[m["file"]],
             "changed": now[m["file"]]["decision"] != new[m["file"]]["decision"] or now[m["file"]]["approver"] != new[m["file"]]["approver"]}
            for m in samples()]


@app.post("/api/reset")
def reset():
    store.reset()
    return {"ok": True}
