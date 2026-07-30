"""Poll each podcast's RSS feed. Hourly normally; every 3 min inside a
podcast's configured fast-poll release window."""
import datetime, logging, time, httpx, feedparser
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo
from app.config import AUDIO_DIR, MAX_EPISODE_AGE_DAYS, SCHEDULE_TZ, RETENTION_DAYS, DOWNLOAD_USER_AGENT
from app.database import get_db, set_status, list_podcasts

log = logging.getLogger(__name__)
FAST_POLL_SECONDS, NORMAL_POLL_SECONDS = 180, 3600
# Look like a normal podcast client rather than a script (see DOWNLOAD_USER_AGENT).
_HEADERS = {"User-Agent": DOWNLOAD_USER_AGENT}

def _fast_weekdays(pod):
    try:
        return {int(x) for x in (pod.get("fast_weekdays") or "").split(",") if x != ""}
    except ValueError:
        return set()

def _in_fast_window(pod, now):
    days = _fast_weekdays(pod)
    hs, he = pod.get("fast_hour_start"), pod.get("fast_hour_end")
    if not days or hs is None or he is None:
        return False
    return now.weekday() in days and hs <= now.hour < he

def get_sleep_seconds():
    """Fast cadence if ANY active podcast is currently in its release window."""
    now = datetime.datetime.now(ZoneInfo(SCHEDULE_TZ))
    with get_db() as db:
        pods = list_podcasts(db, active_only=True)
    if any(_in_fast_window(p, now) for p in pods):
        return FAST_POLL_SECONDS
    return NORMAL_POLL_SECONDS

def is_too_old(pub_date_str: str) -> bool:
    """Return True if the episode is older than MAX_EPISODE_AGE_DAYS."""
    if not MAX_EPISODE_AGE_DAYS or not pub_date_str:
        return False
    try:
        pub = parsedate_to_datetime(pub_date_str)
        # A "-0000" timezone (common in these feeds) yields a naive datetime;
        # treat it as UTC so the comparison below doesn't raise.
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=datetime.timezone.utc)
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=MAX_EPISODE_AGE_DAYS)
        return pub < cutoff
    except Exception:
        log.warning("Could not parse pub_date %r; treating as not-too-old", pub_date_str)
        return False

def stable_guid(entry):
    """Return a dedup key that survives Megaphone rewriting URLs (strip the
    ?updated= query so the same episode always maps to the same key)."""
    raw = entry.get("id") or entry.get("link") or ""
    if raw.startswith("http"):
        parts = urlsplit(raw)
        raw = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return raw

def fetch_feed(url):
    resp = httpx.get(url, timeout=30, follow_redirects=True, headers=_HEADERS)
    resp.raise_for_status()
    return feedparser.parse(resp.text)

def download_episode(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s", url)
    with httpx.stream("GET", url, timeout=600, follow_redirects=True, headers=_HEADERS) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
    return dest

def _delete_files(row):
    for p in (row["raw_audio_path"], row["clean_audio_path"], row["transcript_path"]):
        if p:
            try: Path(p).unlink(missing_ok=True)
            except OSError as e: log.warning("Could not delete %s: %s", p, e)

def cleanup_old_episodes():
    """Remove downloaded episodes past the download age limit, and published
    episodes past the retention window (deleted RETENTION_DAYS after publish)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    with get_db() as db:
        rows = db.execute(
            "SELECT id,title,pub_date,status,published_at,raw_audio_path,"
            "clean_audio_path,transcript_path FROM episodes"
        ).fetchall()
        for row in rows:
            drop = False
            if MAX_EPISODE_AGE_DAYS and is_too_old(row["pub_date"]):
                drop = True
            elif RETENTION_DAYS and row["status"] == "published" and row["published_at"]:
                try:
                    pubd = datetime.datetime.fromisoformat(row["published_at"]).replace(tzinfo=datetime.timezone.utc)
                    if now - pubd > datetime.timedelta(days=RETENTION_DAYS):
                        drop = True
                except ValueError:
                    pass
            if not drop:
                continue
            _delete_files(row)
            db.execute("DELETE FROM episodes WHERE id=?", (row["id"],))
            log.info("Removed episode: %s (%s)", row["title"], row["pub_date"])

def poll_podcast(db, pod):
    feed = fetch_feed(pod["source_rss"])
    for entry in feed.entries:
        guid = stable_guid(entry)
        if not guid:
            continue
        pub_date = entry.get("published", "")
        if is_too_old(pub_date):
            continue
        if db.execute("SELECT id FROM episodes WHERE guid=?", (guid,)).fetchone():
            continue
        audio_url = next((e.href for e in entry.get("enclosures", []) if "audio" in e.get("type", "")), None)
        if not audio_url:
            continue
        title = entry.get("title", "Unknown")
        duration_secs = None
        if entry.get("itunes_duration"):
            parts = str(entry.itunes_duration).split(":")
            try:
                if len(parts) == 3: duration_secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                elif len(parts) == 2: duration_secs = int(parts[0]) * 60 + float(parts[1])
                else: duration_secs = float(parts[0])
            except ValueError: pass
        safe = guid.replace("/", "_").replace(":", "_")[-60:]
        raw_path = AUDIO_DIR / "raw" / f"{safe}.mp3"
        db.execute(
            "INSERT INTO episodes (podcast_id,guid,title,description,pub_date,original_url,duration_secs,raw_audio_path,status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pod["id"], guid, title, entry.get("summary", ""), pub_date, audio_url, duration_secs, str(raw_path), "downloading"),
        )
        eid = db.execute("SELECT id FROM episodes WHERE guid=?", (guid,)).fetchone()[0]
        log.info("New episode [%s]: %s (id=%s)", pod["name"], title, eid)
        try:
            download_episode(audio_url, raw_path)
            set_status(db, eid, "transcribing")
        except Exception as e:
            set_status(db, eid, "error", str(e))
            log.error("Download failed %s: %s", title, e)

def poll_once():
    with get_db() as db:
        pods = list_podcasts(db, active_only=True)
    for pod in pods:
        try:
            with get_db() as db:
                poll_podcast(db, pod)
        except Exception as e:
            log.error("Poll failed for %s: %s", pod["name"], e)

def run_scheduler():
    log.info("Scheduler started. Fast=%ds Normal=%ds MAX_AGE=%s RETENTION=%s",
             FAST_POLL_SECONDS, NORMAL_POLL_SECONDS, MAX_EPISODE_AGE_DAYS, RETENTION_DAYS)
    while True:
        try:
            poll_once()
            cleanup_old_episodes()
        except Exception as e:
            log.error("Scheduler error: %s", e)
        secs = get_sleep_seconds()
        log.info("Next poll in %dm%ds", secs // 60, secs % 60)
        time.sleep(secs)
