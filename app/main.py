import json, logging, secrets, threading
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, Response, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config import AUDIO_DIR, FEED_TOKEN
from app.database import init_db, get_db, get_episode, set_status
from app.scheduler import run_scheduler, poll_once
from app.transcriber import run_transcriber
from app.detector import detect_ad_segments
from app.processor import process_episode
from app.feed import generate_feed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
app = FastAPI(title="Pivot Ad-Free")
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.on_event("startup")
def startup():
    init_db()
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIO_DIR/"raw").mkdir(exist_ok=True)
    (AUDIO_DIR/"clean").mkdir(exist_ok=True)
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=run_transcriber, daemon=True).start()
    log.info("Pivot Ad-Free started.")

def _check_token(request: Request):
    """Require ?t=<FEED_TOKEN> on public feed/audio requests (no-op if unset)."""
    if not FEED_TOKEN:
        return
    if not secrets.compare_digest(request.query_params.get("t", ""), FEED_TOKEN):
        raise HTTPException(403, "Invalid or missing token")

@app.get("/feed.xml")
def rss_feed(request: Request):
    _check_token(request)
    return Response(content=generate_feed(), media_type="application/rss+xml")

def _serve_audio_file(path: Path, request: Request):
    """Serve an audio file with HTTP Range support (206) so podcast clients and
    the review player can seek without downloading the whole file. This Starlette
    version's FileResponse ignores Range, so we handle it ourselves."""
    if not path.exists(): raise HTTPException(404)
    size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type="audio/mpeg", headers={"Accept-Ranges": "bytes"})
    try:
        rng = range_header.split("=", 1)[1]
        start_s, end_s = rng.split("-", 1)
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else size - 1
    except Exception:
        start, end = 0, size - 1
    start = max(0, start)
    end = min(end, size - 1)
    if start > end:
        raise HTTPException(416, headers={"Content-Range": f"bytes */{size}"})
    length = end - start + 1
    def stream():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk: break
                remaining -= len(chunk)
                yield chunk
    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    return StreamingResponse(stream(), status_code=206, headers=headers, media_type="audio/mpeg")

@app.get("/audio/{filename}")
def serve_audio(filename: str, request: Request):
    _check_token(request)
    return _serve_audio_file(AUDIO_DIR / "clean" / filename, request)

@app.get("/audio/raw/{episode_id}")
def serve_raw_audio(episode_id: int, request: Request):
    """Serve the uncut original audio (used by the review page's player)."""
    with get_db() as db:
        ep = get_episode(db, episode_id)
    if not ep or not ep.get("raw_audio_path"): raise HTTPException(404)
    return _serve_audio_file(Path(ep["raw_audio_path"]), request)

@app.get("/review/{episode_id}/transcript")
def episode_transcript(episode_id: int):
    """Return word-level timestamps so the review UI can show what each cut removes."""
    with get_db() as db:
        ep = get_episode(db, episode_id)
    if not ep or not ep.get("transcript_path"): raise HTTPException(404)
    p = Path(ep["transcript_path"])
    if not p.exists(): raise HTTPException(404)
    return {"words": json.loads(p.read_text()).get("words", [])}

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with get_db() as db:
        rows = db.execute("SELECT id,title,pub_date,status,error_msg FROM episodes ORDER BY id DESC").fetchall()
    return templates.TemplateResponse("index.html", {"request": request, "episodes": [dict(r) for r in rows]})

@app.get("/review/{episode_id}", response_class=HTMLResponse)
def review_page(request: Request, episode_id: int):
    with get_db() as db:
        ep = get_episode(db, episode_id)
    if not ep: raise HTTPException(404)
    if ep["status"] == "pending_review" and not ep["ad_segments"] and ep["transcript_path"]:
        segs = detect_ad_segments(Path(ep["transcript_path"]))
        with get_db() as db:
            db.execute("UPDATE episodes SET ad_segments=? WHERE id=?", (json.dumps(segs), episode_id))
        ep["ad_segments"] = segs
    return templates.TemplateResponse("review.html", {"request": request, "ep": ep})

@app.post("/review/{episode_id}/save")
async def save_segments(request: Request, episode_id: int):
    body = await request.json()
    with get_db() as db:
        db.execute("UPDATE episodes SET ad_segments=? WHERE id=?", (json.dumps(body.get("segments",[])), episode_id))
    return {"ok": True}

@app.post("/review/{episode_id}/publish")
def publish_episode(episode_id: int):
    with get_db() as db:
        ep = get_episode(db, episode_id)
        if not ep: raise HTTPException(404)
        if ep["status"] not in ("pending_review","error","published"):
            raise HTTPException(400, f"Status is '{ep['status']}'")
    threading.Thread(target=process_episode, args=(episode_id,), daemon=True).start()
    return {"ok": True}

@app.post("/admin/poll")
def force_poll():
    threading.Thread(target=poll_once, daemon=True).start()
    return {"ok": True}

@app.post("/admin/redetect/{episode_id}")
def redetect(episode_id: int):
    with get_db() as db:
        ep = get_episode(db, episode_id)
    if not ep: raise HTTPException(404)
    if not ep["transcript_path"] or not Path(ep["transcript_path"]).exists():
        raise HTTPException(400, "No transcript for this episode - retranscribe first")
    segs = detect_ad_segments(Path(ep["transcript_path"]))
    with get_db() as db:
        db.execute("UPDATE episodes SET ad_segments=?, updated_at=datetime('now') WHERE id=?", (json.dumps(segs), episode_id))
    return {"ok": True, "count": len(segs)}

@app.post("/admin/retranscribe/{episode_id}")
def retranscribe(episode_id: int):
    with get_db() as db: set_status(db, episode_id, "transcribing")
    return {"ok": True}
