"""Poll the Pivot RSS feed. Hourly normally; every 3min Tue+Fri 05-06."""
import datetime, logging, time, httpx, feedparser
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from app.config import PIVOT_RSS, AUDIO_DIR, MAX_EPISODE_AGE_DAYS
from app.database import get_db, set_status

log = logging.getLogger(__name__)
RELEASE_WEEKDAYS = {1, 4}
RELEASE_WINDOW_START, RELEASE_WINDOW_END = 5, 6
FAST_POLL_SECONDS, NORMAL_POLL_SECONDS = 180, 3600

def get_sleep_seconds():
    now = datetime.datetime.now()
    if now.weekday() in RELEASE_WEEKDAYS and RELEASE_WINDOW_START <= now.hour < RELEASE_WINDOW_END:
        return FAST_POLL_SECONDS
    return NORMAL_POLL_SECONDS

def is_too_old(pub_date_str: str) -> bool:
    """Return True if the episode is older than MAX_EPISODE_AGE_DAYS."""
    if not MAX_EPISODE_AGE_DAYS or not pub_date_str:
        return False
    try:
        pub = parsedate_to_datetime(pub_date_str)
        # A "-0000" timezone (which the Pivot feed uses) yields a naive
        # datetime; treat it as UTC so the comparison below doesn't raise.
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=datetime.timezone.utc)
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=MAX_EPISODE_AGE_DAYS)
        return pub < cutoff
    except Exception:
        log.warning("Could not parse pub_date %r; treating as not-too-old", pub_date_str)
        return False

def stable_guid(entry):
    """Return a dedup key that survives Megaphone rewriting URLs.

    The feed's guid/link is often a tracking URL whose ?updated=<timestamp>
    query param changes over time. Using it raw makes every episode look new
    on each poll, causing endless re-downloads. Strip the query/fragment so
    the same episode always maps to the same key.
    """
    raw = entry.get("id") or entry.get("link") or ""
    if raw.startswith("http"):
        parts = urlsplit(raw)
        raw = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return raw

def fetch_feed():
    resp = httpx.get(PIVOT_RSS, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return feedparser.parse(resp.text)

def download_episode(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s", url)
    with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
    return dest

def cleanup_old_episodes():
    """Delete audio/transcript files and DB rows for episodes past the age limit."""
    if not MAX_EPISODE_AGE_DAYS:
        return
    with get_db() as db:
        rows = db.execute(
            "SELECT id,title,pub_date,raw_audio_path,clean_audio_path,transcript_path FROM episodes"
        ).fetchall()
        for row in rows:
            if not is_too_old(row["pub_date"]):
                continue
            for p in (row["raw_audio_path"], row["clean_audio_path"], row["transcript_path"]):
                if p:
                    try: Path(p).unlink(missing_ok=True)
                    except OSError as e: log.warning("Could not delete %s: %s", p, e)
            db.execute("DELETE FROM episodes WHERE id=?", (row["id"],))
            log.info("Removed old episode: %s (%s)", row["title"], row["pub_date"])

def poll_once():
    feed = fetch_feed()
    with get_db() as db:
        for entry in feed.entries:
            guid = stable_guid(entry)
            if not guid:
                continue
            pub_date = entry.get("published", "")

            if is_too_old(pub_date):
                log.debug("Skipping old episode: %s (%s)", entry.get("title","?"), pub_date)
                continue

            if db.execute("SELECT id FROM episodes WHERE guid=?", (guid,)).fetchone():
                continue

            audio_url = next((e.href for e in entry.get("enclosures", []) if "audio" in e.get("type","")), None)
            if not audio_url:
                continue

            title = entry.get("title", "Unknown")
            duration_secs = None
            if entry.get("itunes_duration"):
                parts = str(entry.itunes_duration).split(":")
                try:
                    if len(parts)==3: duration_secs = int(parts[0])*3600+int(parts[1])*60+float(parts[2])
                    elif len(parts)==2: duration_secs = int(parts[0])*60+float(parts[1])
                    else: duration_secs = float(parts[0])
                except ValueError: pass

            safe = guid.replace("/","_").replace(":","_")[-60:]
            raw_path = AUDIO_DIR / "raw" / f"{safe}.mp3"
            db.execute(
                "INSERT INTO episodes (guid,title,description,pub_date,original_url,duration_secs,raw_audio_path,status) VALUES (?,?,?,?,?,?,?,?)",
                (guid, title, entry.get("summary",""), pub_date, audio_url, duration_secs, str(raw_path), "downloading"),
            )
            eid = db.execute("SELECT id FROM episodes WHERE guid=?", (guid,)).fetchone()[0]
            log.info("New episode: %s (id=%s)", title, eid)
            try:
                download_episode(audio_url, raw_path)
                set_status(db, eid, "transcribing")
            except Exception as e:
                set_status(db, eid, "error", str(e))
                log.error("Download failed %s: %s", title, e)

def run_scheduler():
    log.info("Scheduler started. Fast=%ds Normal=%ds MAX_EPISODE_AGE_DAYS=%s",
             FAST_POLL_SECONDS, NORMAL_POLL_SECONDS, MAX_EPISODE_AGE_DAYS)
    while True:
        try:
            poll_once()
            cleanup_old_episodes()
        except Exception as e: log.error("Scheduler error: %s", e)
        secs = get_sleep_seconds()
        log.info("Next poll in %dm%ds", secs//60, secs%60)
        time.sleep(secs)
