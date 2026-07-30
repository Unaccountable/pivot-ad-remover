import json, logging, subprocess
from pathlib import Path
from app import proclog
from app.config import AUDIO_DIR, FINGERPRINT_ENABLED
from app.database import get_db, get_episode, get_podcast, set_status

log = logging.getLogger(__name__)

# A learned fingerprint is only useful if the same audio recurs verbatim. Ad
# blocks that merged several distinct reads (multiple reasons) never recur as a
# unit, because dynamic insertion reorders and swaps the individual ads - storing
# them would fill the library with large, mislabeled, unmatchable entries. Same
# for anything longer than a single plausible read.
MAX_LEARN_SECONDS = 180.0

def _learn_fingerprints(ep, raw_path, approved_segments):
    """Fingerprint newly-approved ad segments that represent a single ad read,
    adding them to the library so future episodes - any podcast - can be caught
    by audio alone next time. Best-effort: a failure here never blocks or
    unwinds an already-successful publish."""
    from app.fingerprint import learn_from_segment
    for seg in approved_segments:
        reasons = seg.get("reasons") or []
        if any(str(r).startswith("fingerprint match:") for r in reasons):
            continue  # already came from a fingerprint match, nothing new to learn
        span = seg["end"] - seg["start"]
        if len(reasons) > 1 or span > MAX_LEARN_SECONDS:
            log.debug("Skipping fingerprint learning for composite/long segment "
                      "[%.1f,%.1f] (%d reads, %.0fs)", seg["start"], seg["end"],
                      len(reasons), span)
            continue
        label = reasons[0] if reasons else "ad"
        try:
            with get_db() as db:
                learn_from_segment(db, raw_path, seg["start"], seg["end"], label,
                                    podcast_id=ep.get("podcast_id"), episode_id=ep["id"])
        except Exception as e:
            log.warning("Fingerprint learning failed for episode %s segment [%.1f,%.1f]: %s",
                        ep["id"], seg.get("start", -1), seg.get("end", -1), e)

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
        if FINGERPRINT_ENABLED and approved:
            try:
                _learn_fingerprints(ep, raw_path, approved)
            except Exception as e:
                log.warning("Fingerprint learning pass failed for episode %s: %s", episode_id, e)
    except Exception as e:
        proclog.record(pod_name, ep["title"], "failed", detector, error=str(e))
        raise
