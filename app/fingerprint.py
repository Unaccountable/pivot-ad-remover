"""Audio-fingerprint ad detection.

Matches known ad clips against episode audio using Chromaprint (the AcoustID
fingerprinting library, via its C library through ctypes - no `fpcalc`
binary needed). This is a second, independent detection path alongside the
transcript-based LLM/regex one in llm_detector.py: it works on raw audio, so
it catches reused/programmatic ad reads even when the transcript is
mis-heard, needs no LLM call once a clip is known, and is effectively
instant per match. The library grows automatically - every ad segment a
human approves in review gets fingerprinted after publish (see
learn_from_segment, called from processor.py), so later episodes - even
from a different podcast, since ad networks reuse the same creative across
shows - can be matched by audio alone.

Approach, validated against synthetic + MP3-round-tripped test audio:
Chromaprint reduces audio to a sequence of ~7.8 uint32 "fingerprint items"
per second. To find a known clip inside a longer episode, slide the clip's
fingerprint across the episode's and compute the mean Hamming (bit) distance
at every offset; the offset with the lowest distance is the best alignment.
On real audio, a true match lands around 4-12% bit-error (even after a lossy
MP3 re-encode); unrelated audio sits at 35%+ - a wide, easy-to-threshold gap.
"""
import json, logging, subprocess
import numpy as np
import chromaprint
from app.config import (
    FINGERPRINT_MATCH_THRESHOLD, FINGERPRINT_SAMPLE_RATE, AD_BUFFER_SECONDS, AD_MIN_GAP_SECONDS,
)
from app.database import list_fingerprints, add_fingerprint, record_fingerprint_hit

log = logging.getLogger(__name__)

# Lookup table for vectorized popcount over bytes (0-255 -> bit count).
_POPCOUNT_TABLE = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _popcount32(arr_uint32):
    """Vectorized popcount over an array of uint32 values."""
    return _POPCOUNT_TABLE[arr_uint32.astype(np.uint32).view(np.uint8).reshape(-1, 4)].sum(axis=1)


def _pcm_bytes(path, start=None, end=None, sr=FINGERPRINT_SAMPLE_RATE):
    """Decode audio to raw mono 16-bit PCM via ffmpeg, optionally trimmed to
    [start, end) seconds."""
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", str(max(0.0, start))]
    cmd += ["-i", str(path)]
    if end is not None and start is not None and end > start:
        cmd += ["-t", str(end - start)]
    cmd += ["-f", "s16le", "-ar", str(sr), "-ac", "1", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {r.stderr[-500:].decode(errors='replace')}")
    return r.stdout


def _raw_fingerprint(pcm_bytes, sr=FINGERPRINT_SAMPLE_RATE):
    """Return (fingerprint as np.uint32 array, item_rate in items/sec)."""
    if len(pcm_bytes) < 4:
        return np.array([], dtype=np.uint32), 0.0
    fp = chromaprint.Fingerprinter()
    fp.start(sr, 1)
    fp.feed(pcm_bytes)
    compressed = fp.finish()
    raw, _algo = chromaprint.decode_fingerprint(compressed, base64=True)
    arr = np.array(raw, dtype=np.uint32)
    duration = (len(pcm_bytes) / 2) / sr  # 16-bit mono samples -> seconds
    item_rate = (len(arr) / duration) if duration else 0.0
    return arr, item_rate


def fingerprint_clip(audio_path, start, end):
    """Fingerprint a specific [start, end) window (seconds) of an audio file."""
    return _raw_fingerprint(_pcm_bytes(audio_path, start, end))


def fingerprint_full(audio_path):
    """Fingerprint an entire audio file."""
    return _raw_fingerprint(_pcm_bytes(audio_path))


def _mean_dist(ad_fp, ep_fp, chunk=4000):
    """Average per-position Hamming distance (0-32) of ad_fp slid across
    every possible offset in ep_fp. Chunked so peak memory stays small even
    for multi-hour episodes."""
    L, N = len(ad_fp), len(ep_fp)
    if N < L or L == 0:
        return np.array([])
    windows = np.lib.stride_tricks.sliding_window_view(ep_fp, L)
    out = np.empty(len(windows))
    for i in range(0, len(windows), chunk):
        w = windows[i:i + chunk]
        xor = np.bitwise_xor(w, ad_fp)
        out[i:i + chunk] = _popcount32(xor.reshape(-1)).reshape(xor.shape).mean(axis=1)
    return out


def match_library(db, raw_audio_path, threshold=FINGERPRINT_MATCH_THRESHOLD):
    """Scan an episode's raw audio against the stored ad-fingerprint library.

    Returns segments shaped like detector.py/llm_detector.py produce:
    {"start", "end", "reasons", "approved"}. Bumps hit_count/last_matched_at
    on the db for anything that matched (caller owns the commit via get_db).
    """
    library = list_fingerprints(db)
    if not library:
        return []
    try:
        ep_fp, item_rate = fingerprint_full(raw_audio_path)
    except Exception as e:
        log.warning("Could not fingerprint episode audio %s: %s", raw_audio_path, e)
        return []
    if not len(ep_fp) or not item_rate:
        return []

    segments = []
    for entry in library:
        try:
            ad_fp = np.array(json.loads(entry["fp_json"]), dtype=np.uint32)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not len(ad_fp):
            continue
        dist = _mean_dist(ad_fp, ep_fp)
        if not len(dist):
            continue
        best = int(np.argmin(dist))
        bit_error = float(dist[best]) / 32.0
        if bit_error > threshold:
            continue
        start = max(0.0, best / item_rate - AD_BUFFER_SECONDS)
        end = (best + len(ad_fp)) / item_rate + AD_BUFFER_SECONDS
        label = entry.get("label") or "known ad"
        confidence = max(0, round(100 - 100 * bit_error))
        segments.append({
            "start": round(start, 2), "end": round(end, 2),
            "reasons": [f"fingerprint match: {label} ({confidence}% confidence)"],
            "approved": True,
        })
        record_fingerprint_hit(db, entry["id"])
        log.info("Fingerprint match: %r at %.1fs (bit-error %.1f%%)", label, start, bit_error * 100)

    segments.sort(key=lambda s: s["start"])
    merged = []
    for seg in segments:
        if merged and seg["start"] - merged[-1]["end"] < AD_MIN_GAP_SECONDS:
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
            merged[-1]["reasons"].extend(seg["reasons"])
        else:
            merged.append(seg)
    return merged


def learn_from_segment(db, raw_audio_path, start, end, label, podcast_id=None, episode_id=None):
    """Fingerprint a human-approved ad segment and add it to the library so
    future episodes - any podcast - can be matched by audio alone."""
    if end - start < 3.0:
        return None  # too short to fingerprint reliably
    try:
        ad_fp, _rate = fingerprint_clip(raw_audio_path, start, end)
    except Exception as e:
        log.warning("Could not fingerprint segment [%.1f,%.1f] of %s: %s", start, end, raw_audio_path, e)
        return None
    if len(ad_fp) < 10:
        return None
    return add_fingerprint(
        db, label=(label or "ad")[:120], fp=ad_fp.tolist(),
        duration_secs=round(end - start, 2), podcast_id=podcast_id, episode_id=episode_id,
    )
