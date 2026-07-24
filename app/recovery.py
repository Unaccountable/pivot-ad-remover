"""Resume work interrupted by a container restart.

Episodes can be caught mid-flight when the container stops:
  - 'processing'  : an ffmpeg cut/publish thread was killed -> re-run it.
  - 'downloading' : the download was interrupted -> re-download, then transcribe.
  - 'transcribing': automatically picked up again by the transcriber loop
                    (SELECT status='transcribing'), so no action needed here.

On a genuine failure (not a restart) the resume sets the episode to 'error' so
it isn't retried forever on every boot.
"""
import logging, threading
from pathlib import Path
from app.database import get_db, set_status
from app.processor import process_episode
from app.scheduler import download_episode

log = logging.getLogger(__name__)

def _resume_process(eid):
    try:
        log.info("Recovery: resuming interrupted publish for episode %s", eid)
        process_episode(eid)
    except Exception as e:
        with get_db() as db:
            set_status(db, eid, "error", "resume publish failed: " + str(e))
        log.error("Recovery: resume publish failed for %s: %s", eid, e)

def _resume_download(eid, url, raw_path):
    try:
        log.info("Recovery: resuming interrupted download for episode %s", eid)
        download_episode(url, Path(raw_path))
        with get_db() as db:
            set_status(db, eid, "transcribing")
    except Exception as e:
        with get_db() as db:
            set_status(db, eid, "error", "resume download failed: " + str(e))
        log.error("Recovery: resume download failed for %s: %s", eid, e)

def recover_interrupted():
    with get_db() as db:
        proc = [r["id"] for r in db.execute("SELECT id FROM episodes WHERE status='processing'").fetchall()]
        dl = [dict(r) for r in db.execute(
            "SELECT id,original_url,raw_audio_path FROM episodes WHERE status='downloading'").fetchall()]
        transc = db.execute("SELECT COUNT(*) FROM episodes WHERE status='transcribing'").fetchone()[0]
    if proc or dl or transc:
        log.info("Recovery: %d processing, %d downloading, %d transcribing to resume",
                 len(proc), len(dl), transc)
    for eid in proc:
        threading.Thread(target=_resume_process, args=(eid,), daemon=True).start()
    for r in dl:
        if r["original_url"] and r["raw_audio_path"]:
            threading.Thread(target=_resume_download,
                             args=(r["id"], r["original_url"], r["raw_audio_path"]), daemon=True).start()
        else:
            with get_db() as db:
                set_status(db, r["id"], "error", "interrupted download, no source url")
