"""Processing log for finalized episodes.

Every finalize (success or failure) is recorded two ways from one call:
 1. a human-readable line appended to PROCESSING_LOG_FILE on the NAS, and
 2. a row in the processing_log table so the admin UI can search/filter it.
"""
import logging
from datetime import datetime
from app.config import LOG_DIR, PROCESSING_LOG_FILE
from app.database import get_db

log = logging.getLogger(__name__)

def _fmt_secs(s):
    if not s: return "0s"
    s = int(s)
    return f"{s//60}m{s%60:02d}s" if s >= 60 else f"{s}s"

def record(podcast_name, episode_title, status, detector,
           segments=None, cut_secs=None, final_secs=None, error=None):
    """Write one finalize entry to the NAS log file and the processing_log table."""
    # 1) Human-readable line on the NAS.
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = [ts, podcast_name or "?", f'"{(episode_title or "")[:60]}"',
             (status or "?").upper(), f"detector={detector or '?'}"]
    if status == "published":
        parts.append(f"{segments if segments is not None else '?'} segments")
        if cut_secs is not None and final_secs is not None:
            parts.append(f"cut {_fmt_secs(cut_secs)} → {_fmt_secs(final_secs)}")
    if error:
        parts.append(f"error: {str(error)[:300]}")
    line = "  ".join(parts)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(PROCESSING_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        log.warning("Could not write processing log file: %s", e)

    # 2) Searchable row in the DB.
    try:
        with get_db() as db:
            pid = db.execute("SELECT id FROM podcasts WHERE name=?", (podcast_name,)).fetchone()
            db.execute(
                """INSERT INTO processing_log
                   (podcast_id,podcast_name,episode_title,status,detector,segments,cut_secs,final_secs,error)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (pid["id"] if pid else None, podcast_name, episode_title, status,
                 detector, segments, cut_secs, final_secs, str(error) if error else None),
            )
    except Exception as e:
        log.warning("Could not write processing_log row: %s", e)
