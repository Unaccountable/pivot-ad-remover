import json, logging, threading
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config import AUDIO_DIR
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

@app.get("/feed.xml")
def rss_feed():
    return Response(content=generate_feed(), media_type="application/rss+xml")

@app.get("/audio/{filename}")
def serve_audio(filename: str):
    path = AUDIO_DIR / "clean" / filename
    if not path.exists(): raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg")

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
        if ep["status"] not in ("pending_review","error"):
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
