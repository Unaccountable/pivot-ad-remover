import json, logging, subprocess
from pathlib import Path
from app.config import AUDIO_DIR
from app.database import get_db, get_episode, set_status

log = logging.getLogger(__name__)

def build_keep_segments(ad_segments, duration):
    ad_segs = sorted([(s["start"],s["end"]) for s in ad_segments if s.get("approved",True)])
    keep, cursor = [], 0.0
    for ad_start, ad_end in ad_segs:
        if cursor < ad_start: keep.append((cursor, ad_start))
        cursor = ad_end
    if cursor < duration: keep.append((cursor, duration))
    return keep

def cut_audio(raw_path, keep_segments, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not keep_segments: raise ValueError("No segments to keep")
    parts = [f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[seg{i}]" for i,(s,e) in enumerate(keep_segments)]
    inputs = "".join(f"[seg{i}]" for i in range(len(keep_segments)))
    fc = ";".join(parts) + f";{inputs}concat=n={len(keep_segments)}:v=0:a=1[out]"
    cmd = ["ffmpeg","-y","-i",str(raw_path),"-filter_complex",fc,"-map","[out]","-codec:a","libmp3lame","-q:a","2",str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0: raise RuntimeError(f"ffmpeg failed:\n{r.stderr[-2000:]}")
    return out_path

def process_episode(episode_id):
    with get_db() as db:
        ep = get_episode(db, episode_id)
        if not ep: raise ValueError(f"Episode {episode_id} not found")
        set_status(db, episode_id, "processing")
        raw_path = Path(ep["raw_audio_path"])
        duration = ep["duration_secs"] or 7200.0
        if ep["transcript_path"]:
            try: duration = json.loads(Path(ep["transcript_path"]).read_text()).get("duration", duration)
            except: pass
        keep = build_keep_segments(ep["ad_segments"] or [], duration)
        clean_path = AUDIO_DIR / "clean" / raw_path.name
        cut_audio(raw_path, keep, clean_path)
        db.execute("UPDATE episodes SET clean_audio_path=?, status='published', updated_at=datetime('now') WHERE id=?", (str(clean_path), episode_id))
        log.info("Episode %s published", episode_id)
