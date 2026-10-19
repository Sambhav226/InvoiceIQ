"""InvoiceIQ API + UI. Run: uvicorn app.main:app --reload

Auth: INVOICEIQ_AUTH=firebase verifies a Firebase ID token (Google, email/password or anonymous guest) on every
/api call and scopes all data to that user. Unset (local dev, tests) = one local user, no sign-in."""
import json
import os
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import pipeline, store

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
MAX_BYTES = 10 * 1024 * 1024
AUTH = os.getenv("INVOICEIQ_AUTH") == "firebase"
FIREBASE_PROJECT = os.getenv("FIREBASE_PROJECT") or os.getenv("GCP_PROJECT")
if AUTH and not FIREBASE_PROJECT:  # without an audience, tokens minted by ANY Firebase project would verify
    raise RuntimeError("INVOICEIQ_AUTH=firebase requires FIREBASE_PROJECT (or GCP_PROJECT)")
UPLOADS_PER_10_MIN = 30  # per user: bounds Gemini spend and abuse, generous for a demo
app = FastAPI(title="InvoiceIQ")
pending: dict[str, tuple[bytes, str, str]] = {}  # upload token -> (pdf, filename, uid); one per user, single use
_recent: dict[str, list[float]] = {}  # uid -> upload timestamps (rate limit)


# ---------------------------------------------------------------- auth
_token_cache: dict[str, tuple[float, dict]] = {}  # verified ID token -> (exp, user); tokens live 1 h


def user(authorization: str | None = Header(None)) -> dict:
    if not AUTH:
        return {"uid": "local", "guest": False, "email": None}
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in required")
    tok = authorization[7:]
    hit = _token_cache.get(tok)
    if hit and hit[0] > time.time():
        store.profile(hit[1]["uid"], hit[1]["guest"])  # every request: recreates a deleted profile with the right retention
        return hit[1]
    from google.auth.transport import requests as gr
    from google.oauth2 import id_token
    try:
        claims = id_token.verify_firebase_token(tok, gr.Request(), audience=FIREBASE_PROJECT)
        if claims.get("iss") != f"https://securetoken.google.com/{FIREBASE_PROJECT}":
            raise ValueError("wrong issuer")
    except Exception:
        raise HTTPException(401, "Session expired, please sign in again")
    u = {"uid": claims["sub"], "guest": claims.get("firebase", {}).get("sign_in_provider") == "anonymous",
         "email": claims.get("email")}
    if len(_token_cache) > 500:
        _token_cache.clear()
    _token_cache[tok] = (claims["exp"], u)
    store.profile(u["uid"], u["guest"])  # create on first sight; lifts retention when a guest links an account
    return u


def _stash(pdf: bytes, name: str, uid: str, guest: bool = False) -> dict:
    try:
        store.check_room(uid, guest)
    except store.LimitReached as e:
        raise HTTPException(409, str(e))
    now = time.time()
    times = _recent[uid] = [t for t in _recent.get(uid, []) if t > now - 600] + [now]
    if len(times) > UPLOADS_PER_10_MIN:
        raise HTTPException(429, "Too many invoices in 10 minutes. Try again shortly.")
    for t in [t for t, v in pending.items() if v[2] == uid]:  # one pending upload per user: nobody can evict others'
        pending.pop(t, None)
    while len(pending) >= 200:  # global memory bound, oldest first
        pending.pop(next(iter(pending)), None)
    token = uuid.uuid4().hex
    pending[token] = (pdf, name, uid)
    return {"token": token}


# ---------------------------------------------------------------- public
@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/config")
def config():
    """Public Firebase web config (these values are not secrets; access is enforced by ID-token checks)."""
    if not AUTH:
        return {"auth": False}
    return {"auth": True, "firebase": {"apiKey": os.getenv("FIREBASE_API_KEY"), "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN"),
                                       "projectId": FIREBASE_PROJECT, "appId": os.getenv("FIREBASE_APP_ID")}}


@app.get("/api/samples")
def samples():
    return json.loads((SAMPLES / "manifest.json").read_text())


@app.get("/api/samples/{name}")
def sample_pdf(name: str):
    path = (SAMPLES / name).resolve()
    if path.parent != SAMPLES or path.suffix != ".pdf" or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf")


@app.get("/api/process/{token}")
def process(token: str):
    """Runs the pipeline and streams each stage as Server-Sent Events. EventSource can't send headers, so the
    single-use upload token (minted for an authenticated user) is the credential here."""
    item = pending.pop(token, None)
    if not item:
        raise HTTPException(404, "unknown or already-used upload token")
    pdf, name, uid = item

    def events():
        for kind, payload in pipeline.run(pdf, name, uid=uid):
            yield f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- per-user
@app.get("/api/me")
def me(u: dict = Depends(user)):
    doc = store.profile(u["uid"], u["guest"])
    return {**u, "retention_days": doc["retention_days"], "retention_choices": store.RETENTION_CHOICES}


class Retention(BaseModel):
    days: Literal[7, 30, 90]


@app.put("/api/me/retention")
def put_retention(body: Retention, u: dict = Depends(user)):
    if u["guest"]:
        raise HTTPException(403, "Guest data is always deleted after 24 hours. Sign in to choose.")
    return {"retention_days": store.set_retention(u["uid"], body.days)["retention_days"]}


@app.delete("/api/me/data")
def delete_my_data(u: dict = Depends(user)):
    store.delete_all(u["uid"])
    return {"ok": True}


@app.post("/api/upload")
async def upload(file: UploadFile, u: dict = Depends(user)):
    pdf = await file.read(MAX_BYTES + 1)
    if len(pdf) > MAX_BYTES:
        raise HTTPException(413, "PDF larger than 10 MB")
    return _stash(pdf, file.filename or "upload.pdf", u["uid"], u["guest"])


@app.post("/api/upload/sample/{name}")
def upload_sample(name: str, u: dict = Depends(user)):
    path = (SAMPLES / name).resolve()
    if path.parent != SAMPLES or not path.exists():
        raise HTTPException(404)
    return _stash(path.read_bytes(), name, u["uid"], u["guest"])


@app.get("/api/runs")
def runs(u: dict = Depends(user)):
    return store.listing(u["uid"])


@app.get("/api/runs/{run_id}")
def run(run_id: int, u: dict = Depends(user)):
    r = store.get(u["uid"], run_id)
    if not r or not r["decision"]:
        raise HTTPException(404)
    return r


@app.get("/api/runs/{run_id}/pdf")
def run_pdf(run_id: int, u: dict = Depends(user)):
    pdf = store.get_pdf(u["uid"], run_id)
    if not pdf:
        raise HTTPException(404, "Source PDF not kept (larger than 900 KB, or expired)")
    return Response(pdf, media_type="application/pdf")


class Review(BaseModel):
    action: Literal["approve", "reject"]
    note: str = ""


@app.post("/api/runs/{run_id}/review")
def review(run_id: int, body: Review, u: dict = Depends(user)):
    r = store.get(u["uid"], run_id)
    if not r or r["decision"] != "HOLD":
        raise HTTPException(400, "only HOLD runs can be reviewed")
    return store.review(u["uid"], run_id, body.action, body.note[:500])


@app.get("/api/metrics")
def metrics(u: dict = Depends(user)):
    return store.metrics(u["uid"])


@app.get("/api/policy")
def get_policy(u: dict = Depends(user)):
    return {"policy": store.get_policy(u["uid"]), "defaults": store.Policy()}


@app.put("/api/policy")
def put_policy(p: store.Policy, u: dict = Depends(user)):
    return store.set_policy(u["uid"], p)


@app.post("/api/policy/preview")
def preview_policy(p: store.Policy, u: dict = Depends(user)):
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
    now, new = replay(store.get_policy(u["uid"])), replay(p)
    return [{"file": m["file"], "title": m["title"], "current": now[m["file"]], "proposed": new[m["file"]],
             "changed": now[m["file"]]["decision"] != new[m["file"]]["decision"] or now[m["file"]]["approver"] != new[m["file"]]["approver"]}
            for m in samples()]


@app.post("/api/reset")
def reset(u: dict = Depends(user)):
    store.reset(u["uid"])
    return {"ok": True}
