import json, logging, secrets, threading
from typing import Optional
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, Response, FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config import AUDIO_DIR, ARTWORK_DIR, BASE_URL
from app.database import (
    init_db, get_db, get_episode, set_status, slugify,
    get_podcast, get_podcast_by_slug, get_episode_by_clean_name, list_podcasts,
)
from app.scheduler import run_scheduler, poll_once, fetch_feed
from app.transcriber import run_transcriber
from app.llm_detector import detect_ads
from app.processor import process_episode
from app.feed import generate_feed, _feed_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
app = FastAPI(title="Podcast Ad-Free")
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["feed_path"] = _feed_path
templates.env.globals["base_url"] = BASE_URL
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

@app.on_event("startup")
def startup():
    init_db()
    for sub in ("raw", "clean"):
        (AUDIO_DIR / sub).mkdir(parents=True, exist_ok=True)
    ARTWORK_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=run_transcriber, daemon=True).start()
    log.info("Podcast Ad-Free started.")

# --- public feed + audio (per-podcast token) ----------------------------
def _check_podcast_token(request: Request, pod: dict):
    tok = pod.get("feed_token") or ""
    if not tok:
        return
    if not secrets.compare_digest(request.query_params.get("t", ""), tok):
        raise HTTPException(403, "Invalid or missing token")

def _serve_feed(request, pod):
    if not pod:
        raise HTTPException(404)
    _check_podcast_token(request, pod)
    return Response(content=generate_feed(pod), media_type="application/rss+xml")

@app.get("/feed.xml")
def rss_feed_pivot(request: Request):
    with get_db() as db:
        pod = get_podcast_by_slug(db, "pivot")
    return _serve_feed(request, pod)

@app.get("/feed/{slug}.xml")
def rss_feed_slug(slug: str, request: Request):
    with get_db() as db:
        pod = get_podcast_by_slug(db, slug)
    return _serve_feed(request, pod)

def _serve_audio_file(path: Path, request: Request):
    """Serve an audio file with HTTP Range support (206) so podcast clients and
    the review player can seek without downloading the whole file."""
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
    start = max(0, start); end = min(end, size - 1)
    if start > end:
        raise HTTPException(416, headers={"Content-Range": f"bytes */{size}"})
    length = end - start + 1
    def stream():
        with open(path, "rb") as f:
            f.seek(start); remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk: break
                remaining -= len(chunk); yield chunk
    headers = {"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes",
               "Content-Length": str(length)}
    return StreamingResponse(stream(), status_code=206, headers=headers, media_type="audio/mpeg")

@app.get("/audio/raw/{episode_id}")
def serve_raw_audio(episode_id: int, request: Request):
    """Uncut original audio for the review player (LAN-only via the proxy)."""
    with get_db() as db:
        ep = get_episode(db, episode_id)
    if not ep or not ep.get("raw_audio_path"): raise HTTPException(404)
    return _serve_audio_file(Path(ep["raw_audio_path"]), request)

@app.get("/audio/{filename}")
def serve_audio(filename: str, request: Request):
    with get_db() as db:
        ep = get_episode_by_clean_name(db, filename)
        pod = get_podcast(db, ep["podcast_id"]) if ep else None
    if not ep or not pod: raise HTTPException(404)
    _check_podcast_token(request, pod)
    return _serve_audio_file(AUDIO_DIR / "clean" / filename, request)

@app.get("/artwork/{slug}")
def serve_artwork(slug: str):
    with get_db() as db:
        pod = get_podcast_by_slug(db, slug)
    if not pod or not pod.get("image_file"): raise HTTPException(404)
    p = ARTWORK_DIR / pod["image_file"]
    if not p.exists(): raise HTTPException(404)
    return FileResponse(p)

# --- episodes / review --------------------------------------------------
@app.get("/review/{episode_id}/transcript")
def episode_transcript(episode_id: int):
    with get_db() as db:
        ep = get_episode(db, episode_id)
    if not ep or not ep.get("transcript_path"): raise HTTPException(404)
    p = Path(ep["transcript_path"])
    if not p.exists(): raise HTTPException(404)
    return {"words": json.loads(p.read_text()).get("words", [])}

def _opt_int(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else None

@app.get("/", response_class=HTMLResponse)
def index(request: Request, podcast_id: str = ""):
    podcast_id = _opt_int(podcast_id)
    with get_db() as db:
        pods = list_podcasts(db)
        sql = ("SELECT e.id,e.title,e.pub_date,e.status,e.error_msg,e.detector,p.name AS pod "
               "FROM episodes e LEFT JOIN podcasts p ON p.id=e.podcast_id")
        args = []
        if podcast_id:
            sql += " WHERE e.podcast_id=?"; args.append(podcast_id)
        sql += " ORDER BY e.id DESC"
        rows = db.execute(sql, args).fetchall()
    return templates.TemplateResponse("index.html", {"request": request,
        "episodes": [dict(r) for r in rows], "podcasts": pods, "selected": podcast_id})

@app.get("/review/{episode_id}", response_class=HTMLResponse)
def review_page(request: Request, episode_id: int):
    with get_db() as db:
        ep = get_episode(db, episode_id)
    if not ep: raise HTTPException(404)
    if ep["status"] == "pending_review" and not ep["ad_segments"] and ep["transcript_path"]:
        segs, detector = detect_ads(Path(ep["transcript_path"]))
        with get_db() as db:
            db.execute("UPDATE episodes SET ad_segments=?, detector=? WHERE id=?",
                       (json.dumps(segs), detector, episode_id))
        ep["ad_segments"] = segs; ep["detector"] = detector
    return templates.TemplateResponse("review.html", {"request": request, "ep": ep})

@app.post("/review/{episode_id}/save")
async def save_segments(request: Request, episode_id: int):
    body = await request.json()
    with get_db() as db:
        db.execute("UPDATE episodes SET ad_segments=? WHERE id=?",
                   (json.dumps(body.get("segments", [])), episode_id))
    return {"ok": True}

@app.post("/review/{episode_id}/publish")
def publish_episode(episode_id: int):
    with get_db() as db:
        ep = get_episode(db, episode_id)
        if not ep: raise HTTPException(404)
        if ep["status"] not in ("pending_review", "error", "published"):
            raise HTTPException(400, f"Status is '{ep['status']}'")
    threading.Thread(target=process_episode, args=(episode_id,), daemon=True).start()
    return {"ok": True}

@app.post("/admin/redetect/{episode_id}")
def redetect(episode_id: int):
    with get_db() as db:
        ep = get_episode(db, episode_id)
    if not ep: raise HTTPException(404)
    if not ep["transcript_path"] or not Path(ep["transcript_path"]).exists():
        raise HTTPException(400, "No transcript for this episode - retranscribe first")
    segs, detector = detect_ads(Path(ep["transcript_path"]))
    with get_db() as db:
        db.execute("UPDATE episodes SET ad_segments=?, detector=?, updated_at=datetime('now') WHERE id=?",
                   (json.dumps(segs), detector, episode_id))
    return {"ok": True, "count": len(segs), "detector": detector}

@app.post("/admin/poll")
def force_poll():
    threading.Thread(target=poll_once, daemon=True).start()
    return {"ok": True}

@app.post("/admin/retranscribe/{episode_id}")
def retranscribe(episode_id: int):
    with get_db() as db: set_status(db, episode_id, "transcribing")
    return {"ok": True}

# --- podcasts admin -----------------------------------------------------
def _unique_slug(db, base):
    slug, n = base, 2
    while db.execute("SELECT 1 FROM podcasts WHERE slug=?", (slug,)).fetchone():
        slug = f"{base}-{n}"; n += 1
    return slug

def _fetch_meta(url):
    """Best-effort podcast metadata from the source feed."""
    d = fetch_feed(url)
    f = d.feed
    image = ""
    if isinstance(f.get("image"), dict):
        image = f["image"].get("href") or f["image"].get("url") or ""
    if not image and isinstance(f.get("itunes_image"), dict):
        image = f["itunes_image"].get("href", "")
    owner = ""
    if isinstance(f.get("author_detail"), dict):
        owner = f["author_detail"].get("email", "")
    category = "News"
    if f.get("tags"):
        category = f["tags"][0].get("term") or category
    return {
        "title": f.get("title", "Podcast"),
        "author": f.get("author") or f.get("itunes_author") or "",
        "description": f.get("subtitle") or f.get("summary") or "",
        "image_url": image, "owner_email": owner, "category": category,
    }

def _parse_fast(weekdays, hs, he):
    wd = ",".join(x.strip() for x in (weekdays or "").replace(" ", "").split(",") if x.strip().isdigit())
    try: hs = int(hs) if hs not in (None, "") else None
    except ValueError: hs = None
    try: he = int(he) if he not in (None, "") else None
    except ValueError: he = None
    return (wd or None), hs, he

@app.get("/podcasts", response_class=HTMLResponse)
def podcasts_page(request: Request):
    with get_db() as db:
        pods = list_podcasts(db)
    return templates.TemplateResponse("podcasts.html", {"request": request, "podcasts": pods})

@app.post("/podcasts")
def create_podcast(source_rss: str = Form(...), name: str = Form(""),
                   fast_weekdays: str = Form(""), fast_hour_start: str = Form(""),
                   fast_hour_end: str = Form("")):
    try:
        meta = _fetch_meta(source_rss)
    except Exception as e:
        raise HTTPException(400, f"Could not read feed: {e}")
    title = (name.strip() or meta["title"])
    wd, hs, he = _parse_fast(fast_weekdays, fast_hour_start, fast_hour_end)
    with get_db() as db:
        slug = _unique_slug(db, slugify(title))
        db.execute(
            """INSERT INTO podcasts
               (slug,name,source_rss,feed_token,title,description,author,category,
                owner_email,image_url,fast_weekdays,fast_hour_start,fast_hour_end,active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (slug, title, source_rss, secrets.token_urlsafe(24), title, meta["description"],
             meta["author"], meta["category"], meta["owner_email"], meta["image_url"], wd, hs, he),
        )
    return RedirectResponse("/podcasts", status_code=303)

@app.get("/podcasts/{pid}/edit", response_class=HTMLResponse)
def edit_podcast_page(request: Request, pid: int):
    with get_db() as db:
        pod = get_podcast(db, pid)
    if not pod: raise HTTPException(404)
    return templates.TemplateResponse("podcast_edit.html", {"request": request, "pod": pod})

@app.post("/podcasts/{pid}")
def update_podcast(pid: int, name: str = Form(...), source_rss: str = Form(...),
                   author: str = Form(""), category: str = Form("News"),
                   owner_email: str = Form(""), description: str = Form(""),
                   image_url: str = Form(""), fast_weekdays: str = Form(""),
                   fast_hour_start: str = Form(""), fast_hour_end: str = Form(""),
                   active: str = Form("")):  # unchecked checkbox omits the field
    wd, hs, he = _parse_fast(fast_weekdays, fast_hour_start, fast_hour_end)
    with get_db() as db:
        if not get_podcast(db, pid): raise HTTPException(404)
        db.execute(
            """UPDATE podcasts SET name=?,title=?,source_rss=?,author=?,category=?,owner_email=?,
               description=?,image_url=?,fast_weekdays=?,fast_hour_start=?,fast_hour_end=?,active=? WHERE id=?""",
            (name, name, source_rss, author, category, owner_email, description, image_url,
             wd, hs, he, 1 if active in ("on", "1", "true") else 0, pid),
        )
    return RedirectResponse("/podcasts", status_code=303)

@app.post("/podcasts/{pid}/artwork")
async def upload_artwork(pid: int, artwork: UploadFile = File(...)):
    ext = Path(artwork.filename or "").suffix.lower()
    if ext not in IMG_EXT:
        raise HTTPException(400, f"Unsupported image type {ext}")
    with get_db() as db:
        pod = get_podcast(db, pid)
    if not pod: raise HTTPException(404)
    data = await artwork.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 10 MB)")
    ARTWORK_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{pod['slug']}{ext}"
    (ARTWORK_DIR / fname).write_bytes(data)
    with get_db() as db:
        db.execute("UPDATE podcasts SET image_file=? WHERE id=?", (fname, pid))
    return RedirectResponse("/podcasts", status_code=303)

@app.post("/podcasts/{pid}/regenerate-token")
def regenerate_token(pid: int):
    with get_db() as db:
        db.execute("UPDATE podcasts SET feed_token=? WHERE id=?", (secrets.token_urlsafe(24), pid))
    return RedirectResponse("/podcasts", status_code=303)

@app.post("/podcasts/{pid}/delete")
def delete_podcast(pid: int):
    with get_db() as db:
        pod = get_podcast(db, pid)
        if not pod: raise HTTPException(404)
        rows = db.execute("SELECT raw_audio_path,clean_audio_path,transcript_path FROM episodes WHERE podcast_id=?", (pid,)).fetchall()
        for r in rows:
            for p in (r["raw_audio_path"], r["clean_audio_path"], r["transcript_path"]):
                if p:
                    try: Path(p).unlink(missing_ok=True)
                    except OSError: pass
        db.execute("DELETE FROM episodes WHERE podcast_id=?", (pid,))
        db.execute("DELETE FROM podcasts WHERE id=?", (pid,))
    if pod.get("image_file"):
        try: (ARTWORK_DIR / pod["image_file"]).unlink(missing_ok=True)
        except OSError: pass
    return RedirectResponse("/podcasts", status_code=303)

# --- processing logs ----------------------------------------------------
@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, q: str = "", status: str = "", podcast_id: str = ""):
    podcast_id = _opt_int(podcast_id)
    with get_db() as db:
        pods = list_podcasts(db)
        sql = "SELECT * FROM processing_log WHERE 1=1"; args = []
        if q:
            sql += " AND episode_title LIKE ?"; args.append(f"%{q}%")
        if status:
            sql += " AND status=?"; args.append(status)
        if podcast_id:
            sql += " AND podcast_id=?"; args.append(podcast_id)
        sql += " ORDER BY id DESC LIMIT 500"
        rows = [dict(r) for r in db.execute(sql, args).fetchall()]
    return templates.TemplateResponse("logs.html", {"request": request, "logs": rows,
        "podcasts": pods, "q": q, "status": status, "selected": podcast_id})
