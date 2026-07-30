import json, logging, time
from pathlib import Path
from app.config import WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE, WHISPER_CPU_THREADS, DATA_DIR, AUTO_PUBLISH
from app.database import get_db, set_status
from app.llm_detector import detect_ads
from app.processor import process_episode

log = logging.getLogger(__name__)
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
_model = None

def get_model():
    from faster_whisper import WhisperModel
    return WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE,
                        cpu_threads=WHISPER_CPU_THREADS, num_workers=1)

def transcribe_episode(episode):
    global _model
    if _model is None: _model = get_model()
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = Path(episode["raw_audio_path"])
    out = TRANSCRIPT_DIR / f"{audio_path.stem}.json"
    log.info("Transcribing: %s", episode["title"])
    t0 = time.time()
    segments, info = _model.transcribe(str(audio_path), word_timestamps=True, beam_size=5, vad_filter=True)
    words = [{"start": round(w.start,3), "end": round(w.end,3), "word": w.word}
             for seg in segments for w in (seg.words or [])]
    out.write_text(json.dumps({"episode_id": episode["id"], "title": episode["title"],
                                "language": info.language, "duration": info.duration, "words": words}, indent=2))
    log.info("Transcribed in %.0fs, %d words", time.time()-t0, len(words))
    return out

def run_transcriber():
    while True:
        with get_db() as db:
            row = db.execute("SELECT * FROM episodes WHERE status='transcribing' ORDER BY id LIMIT 1").fetchone()
        if not row:
            time.sleep(30)
            continue
        ep = dict(row)
        # Transcribe (long-running) without holding a DB connection open.
        try:
            tp = transcribe_episode(ep)
        except Exception as e:
            with get_db() as db:
                set_status(db, ep["id"], "error", str(e))
            continue

        if AUTO_PUBLISH:
            segs, detector = detect_ads(tp, ep["raw_audio_path"])
        else:
            segs, detector = None, None
        with get_db() as db:
            if AUTO_PUBLISH:
                db.execute("UPDATE episodes SET transcript_path=?, ad_segments=?, detector=?, status='pending_review', updated_at=datetime('now') WHERE id=?",
                           (str(tp), json.dumps(segs), detector, ep["id"]))
            else:
                db.execute("UPDATE episodes SET transcript_path=?, status='pending_review', updated_at=datetime('now') WHERE id=?",
                           (str(tp), ep["id"]))

        # Auto-publish: cut the ads and publish immediately. The episode stays
        # editable afterward via the review page (re-save republishes).
        if AUTO_PUBLISH:
            try:
                process_episode(ep["id"])
                log.info("Auto-published episode %s via %s (%d ad segments)", ep["id"], detector, len(segs))
            except Exception as e:
                with get_db() as db:
                    set_status(db, ep["id"], "error", "auto-publish failed: " + str(e))
                log.error("Auto-publish failed for %s: %s", ep["id"], e)
