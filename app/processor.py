import json, logging, subprocess
from pathlib import Path
from app import proclog
from app.config import AUDIO_DIR
from app.database import get_db, get_episode, get_podcast, set_status

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

def _probe_duration(path):
    try:
        r = subprocess.run(["ffprobe","-v","quiet","-show_entries","format=duration","-of","csv=p=0",str(path)],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return None

def process_episode(episode_id):
    with get_db() as db:
        ep = get_episode(db, episode_id)
        if not ep: raise ValueError(f"Episode {episode_id} not found")
        pod = get_podcast(db, ep.get("podcast_id"))
        set_status(db, episode_id, "processing")
    pod_name = pod["name"] if pod else "?"
    detector = ep.get("detector") or "manual"
    try:
        raw_path = Path(ep["raw_audio_path"])
        duration = ep["duration_secs"] or 7200.0
        if ep["transcript_path"]:
            try: duration = json.loads(Path(ep["transcript_path"]).read_text()).get("duration", duration)
            except Exception: pass
        segs = ep["ad_segments"] or []
        approved = [s for s in segs if s.get("approved", True)]
        cut_secs = sum(max(0.0, s["end"] - s["start"]) for s in approved)
        keep = build_keep_segments(segs, duration)
        clean_path = AUDIO_DIR / "clean" / raw_path.name
        cut_audio(raw_path, keep, clean_path)
        final_secs = _probe_duration(clean_path) or max(0.0, duration - cut_secs)
        with get_db() as db:
            db.execute(
                "UPDATE episodes SET clean_audio_path=?, status='published', "
                "published_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
                (str(clean_path), episode_id),
            )
        proclog.record(pod_name, ep["title"], "published", detector,
                       segments=len(approved), cut_secs=cut_secs, final_secs=final_secs)
        log.info("Episode %s published", episode_id)
    except Exception as e:
        proclog.record(pod_name, ep["title"], "failed", detector, error=str(e))
        raise
