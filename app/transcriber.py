import json, logging, time
from pathlib import Path
from app.config import WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE, WHISPER_CPU_THREADS, DATA_DIR
from app.database import get_db, set_status

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
            if row:
                ep = dict(row)
                try:
                    tp = transcribe_episode(ep)
                    db.execute("UPDATE episodes SET transcript_path=?, status='pending_review', updated_at=datetime('now') WHERE id=?", (str(tp), ep["id"]))
                except Exception as e:
                    set_status(db, ep["id"], "error", str(e))
            else:
                time.sleep(30)
