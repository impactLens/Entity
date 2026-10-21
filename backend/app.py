"""Entity Highlighter API.

POST /v1/extract   entity spans for a list of texts (long texts are chunked)
GET  /v1/stats     load and memory counters           (needs API key)
POST /admin/reset  clear rate-limit state and counters (needs API key)
GET  /health       liveness probe
"""
import gc
import hmac
import os
import threading
import time
from collections import defaultdict, deque
from typing import List, Optional

import spacy
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import ner_core as core


def _int(name, default):
    return int(os.getenv(name, default))


# ---------------- configuration (environment) ----------------
API_KEY = os.getenv("NER_API_KEY", "")
MODEL = os.getenv("SPACY_MODEL", "en_core_web_sm")
ENABLE_NLTK = os.getenv("ENABLE_NLTK", "1") == "1"   # 0 saves memory and time
ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

MAX_TEXTS = _int("MAX_TEXTS", 500)
MAX_TEXT_CHARS = _int("MAX_TEXT_CHARS", 300_000)     # one text (chunked inside)
MAX_TOTAL_CHARS = _int("MAX_TOTAL_CHARS", 600_000)   # whole request
CHUNK_CHARS = _int("CHUNK_CHARS", 5_000)             # model input size
BATCH_CHUNKS = _int("BATCH_CHUNKS", 16)              # chunks per lock hold
MAX_REQUEST_S = _int("MAX_REQUEST_S", 120)           # per-request time budget

MAX_INFLIGHT = _int("MAX_INFLIGHT", 10)              # analyses per worker
QUEUE_WAIT_S = float(os.getenv("QUEUE_WAIT_S", "30"))
RATE_PER_MIN = _int("RATE_LIMIT_PER_MIN", 600)       # per client IP, per worker

# ---------------- models and shared state ----------------
NLP = spacy.load(MODEL, disable=["tagger", "parser", "attribute_ruler",
                                 "lemmatizer"])
LEX_RX = core.compile_lexicon(core.LEXICON)
if ENABLE_NLTK:
    core.ensure_nltk()

NLP_LOCK = threading.Lock()    # spaCy: one batch at a time per worker
NLTK_LOCK = threading.Lock()   # NLTK models are shared objects
SLOTS = threading.BoundedSemaphore(MAX_INFLIGHT)
STATS_LOCK = threading.Lock()
STATS = {"started": time.time(), "served": 0, "rejected": 0, "errors": 0,
         "inflight": 0}
_hits = defaultdict(deque)


def bump(key, delta=1):
    with STATS_LOCK:
        STATS[key] += delta


# ---------------- app ----------------
app = FastAPI(title="Entity Highlighter API", version="1.2.0",
              docs_url=None if API_KEY else "/docs", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS,
                   allow_methods=["POST", "GET"],
                   allow_headers=["Content-Type", "X-API-Key"],
                   allow_credentials=False)


@app.middleware("http")
async def limit_body(request: Request, call_next):
    size = request.headers.get("content-length")
    if size and size.isdigit() and int(size) > MAX_TOTAL_CHARS * 6:
        return JSONResponse({"detail": "request body too large"}, status_code=413)
    return await call_next(request)


def auth(request: Request, x_api_key: str = Header(default="")):
    if API_KEY and not hmac.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(401, "invalid api key")
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ident = fwd or (request.client.host if request.client else "?")
    now = time.monotonic()
    if len(_hits) > 10_000:
        _hits.clear()
    q = _hits[ident]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_PER_MIN:
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "10"})
    q.append(now)


class Options(BaseModel):
    nltk: bool = True                    # NLTK pass (ignored if ENABLE_NLTK=0)
    phrases: bool = True                 # noun-phrase spans (needs nltk)
    labels: Optional[List[str]] = None   # keep only these labels


class ExtractRequest(BaseModel):
    texts: List[str]
    options: Options = Options()


# ---------------- analysis ----------------
def analyze(texts, opts, deadline):
    """Return one span list per text. Offsets are UTF-16 units."""
    use_nltk = ENABLE_NLTK and opts.nltk

    # 1. Rule-based spans (fast) on each whole text.
    per = [core.lexicon_spans(t, core.LEXICON, LEX_RX) + core.datetime_spans(t)
           for t in texts]

    # 2. Model spans on chunks, in small batches so other users can interleave.
    work = [(i, off, chunk)
            for i, t in enumerate(texts) if t.strip()
            for off, chunk in core.chunk_text(t, CHUNK_CHARS)]
    for b in range(0, len(work), BATCH_CHUNKS):
        if time.monotonic() > deadline:
            raise HTTPException(504, "time budget exceeded; send shorter text")
        batch = work[b:b + BATCH_CHUNKS]
        with NLP_LOCK:
            docs = list(NLP.pipe([c for _, _, c in batch], batch_size=16))
        for (i, off, chunk), doc in zip(batch, docs):
            spans = core.doc_spans(doc)
            if use_nltk:
                with NLTK_LOCK:
                    spans += core.nltk_spans(chunk, phrases=opts.phrases)
            per[i] += core.shift(spans, off)

    # 3. Resolve overlaps, filter, convert offsets for JavaScript.
    keep = {l.upper() for l in opts.labels} if opts.labels else None
    out = []
    for t, spans in zip(texts, per):
        spans = core.resolve(spans)
        if keep:
            spans = [s for s in spans if s["label"] in keep]
        out.append(core.to_utf16_offsets(t, spans))
    return out


def warm_up():
    """Load lazy models at startup so no user request pays for them."""
    try:
        analyze(["Dr. Amina Yusuf of Oxford University visited Berlin on "
                 "3 March 2026 at 9:30 AM."], Options(), time.monotonic() + 120)
        print("warm-up ok", flush=True)
    except Exception as exc:
        print(f"warm-up failed: {exc!r}", flush=True)


warm_up()


def _rss_mb():
    try:
        pages = int(open("/proc/self/statm").read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576)
    except Exception:
        return None


# ---------------- routes ----------------
@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL, "nltk": ENABLE_NLTK, "pid": os.getpid()}


@app.get("/v1/stats", dependencies=[Depends(auth)])
def stats():
    with STATS_LOCK:
        snap = dict(STATS)
    snap.update(pid=os.getpid(), rss_mb=_rss_mb(), max_inflight=MAX_INFLIGHT,
                uptime_s=round(time.time() - snap.pop("started")))
    return snap


@app.post("/admin/reset", dependencies=[Depends(auth)])
def reset():
    """Soft reset of THIS worker. For a full restart use ./server.sh reset."""
    _hits.clear()
    with STATS_LOCK:
        STATS.update(served=0, rejected=0, errors=0)
    gc.collect()
    return {"ok": True, "pid": os.getpid()}


@app.post("/v1/extract", dependencies=[Depends(auth)])
def extract(req: ExtractRequest):
    texts = req.texts
    if len(texts) > MAX_TEXTS:
        raise HTTPException(413, f"max {MAX_TEXTS} texts per request")
    if any(len(t) > MAX_TEXT_CHARS for t in texts):
        raise HTTPException(413, f"max {MAX_TEXT_CHARS} chars per text")
    if sum(len(t) for t in texts) > MAX_TOTAL_CHARS:
        raise HTTPException(413, f"max {MAX_TOTAL_CHARS} chars per request")
    if not SLOTS.acquire(timeout=QUEUE_WAIT_S):
        bump("rejected")
        raise HTTPException(503, "server busy, retry shortly",
                            headers={"Retry-After": "3"})
    bump("inflight")
    t0 = time.perf_counter()
    try:
        results = analyze(texts, req.options, time.monotonic() + MAX_REQUEST_S)
        bump("served")
    except HTTPException:
        raise
    except Exception:
        bump("errors")
        raise
    finally:
        bump("inflight", -1)
        SLOTS.release()
    return {"results": results,
            "meta": {"model": MODEL, "texts": len(texts), "pid": os.getpid(),
                     "ms": round((time.perf_counter() - t0) * 1000)}}
