"""Ad detection dispatcher.

detect_ads() returns (segments, detector_label). It combines two independent
detection paths and merges their results:

  1. Audio fingerprint matching (fingerprint.py) - runs first, against the
     raw episode audio, if there's a library of previously-confirmed ad
     clips to check against. No transcript or LLM call needed; matches are
     effectively free and instant.
  2. Transcript-based detection - the LLM provider when configured and
     reachable, automatically falling back to the regex detector on any
     error or missing key, so publishing is never blocked.

The label records exactly which path(s) ran and how many segments each
contributed, e.g. "fingerprint(2)+llm(haiku-4.5)", "regex(fallback: no API key)".
"""
import json, logging, re, subprocess
from bisect import bisect_right, bisect_left
from pathlib import Path
from app.config import (
    LLM_PROVIDER, ANTHROPIC_API_KEY, LLM_MODEL, AD_BUFFER_SECONDS, AD_MIN_GAP_SECONDS,
    FINGERPRINT_ENABLED,
)
from app.detector import detect_ad_segments  # regex fallback

log = logging.getLogger(__name__)

# How far from an LLM-guessed boundary to look for a real silence gap to snap to,
# and how quiet/long a gap must be to count. Pivot (like most shows) has a brief
# silence or music bed between segments, which is a much more precise edit point
# than trusting the LLM's exact second.
SILENCE_SEARCH_WINDOW = 10.0
SILENCE_NOISE_DB = "-30dB"
SILENCE_MIN_DUR = 0.3
# Shorter than this and a "segment" can't be a real ad; emitting it would cut
# nothing while still counting as a detection.
MIN_SEGMENT_SECONDS = 1.0

SYSTEM_PROMPT = (
    "You identify advertising in a podcast transcript. The transcript is given as "
    "lines each prefixed with a start time in seconds, e.g. `[123.4] words...`.\n\n"
    "Mark every ADVERTISING / SPONSORSHIP segment: host-read ads, dynamically "
    "inserted ads, 'brought to you by', 'support for this show comes from', promo "
    "codes, discount offers, and calls to visit a sponsor's website. Include the "
    "FULL span of each ad read, from the first sponsor word to the last, including "
    "'we'll be right back' lead-ins and the trailing legal/terms lines.\n\n"
    "Also mark CROSS-PROMOTIONAL TRAILERS for other podcasts/shows as advertising, "
    "even if they never mention a brand or product. These are self-contained "
    "promotional segments, not the hosts' own commentary: they are usually "
    "introduced by someone who is not a regular host of this show, describe a "
    "different show's premise or guests, and close with that show's own sign-off "
    "('I'm ___, and this is ___', 'catch us every ___', 'wherever you get your "
    "podcasts'). This applies even if the trailer's subject matter (e.g. politics, "
    "pop culture) sounds like it could be organic content.\n\n"
    "Do NOT mark editorial content: the hosts organically discussing, reacting to, "
    "or reading news about another show, or playing an actual clip/excerpt from "
    "another show or interview as part of their own commentary. The distinguishing "
    "signal is who is speaking and why, not the topic: the show's regular hosts "
    "talking in their own voice is never an ad, even if they're talking about "
    "another podcast, a movie, or a guest's other work.\n\n"
    "Do NOT mark the show's own intro/outro, credits, or listener housekeeping "
    "(social handles, call-in numbers) unless they are part of a sponsor read.\n\n"
    "Respond with ONLY a JSON object, no prose, of the form:\n"
    '{"segments":[{"start_sec":<number>,"end_sec":<number>,"reason":"<short label>"}]}\n'
    "Use the timestamps from the transcript lines. If there are no ads, return "
    '{"segments":[]}.'
)

_MODEL_SHORT = {"claude-haiku-4-5": "haiku-4.5", "claude-sonnet-5": "sonnet-5",
                "claude-opus-4-8": "opus-4.8"}

def _short_model():
    return _MODEL_SHORT.get(LLM_MODEL, LLM_MODEL)

def _load_words(transcript_path):
    data = json.loads(Path(transcript_path).read_text())
    return data.get("words", []), (data.get("duration") or 0)

def _build_prompt(words):
    """Group words into ~1-line-per-8s timestamped lines for the model."""
    lines, cur, line_start = [], [], None
    for w in words:
        if line_start is None:
            line_start = w["start"]
        cur.append(w["word"].strip())
        if w["start"] - line_start >= 8.0:
            lines.append(f"[{line_start:.1f}] " + " ".join(cur))
            cur, line_start = [], None
    if cur:
        lines.append(f"[{line_start:.1f}] " + " ".join(cur))
    return "\n".join(lines)

def _snap(words, starts, ends, t, upper):
    """Snap time t to the nearest word boundary (start if upper=False, end if True)."""
    if not words:
        return t
    if upper:
        i = min(bisect_left(ends, t), len(ends) - 1)
        return ends[i]
    i = max(bisect_right(starts, t) - 1, 0)
    return starts[i]

_SILENCE_RE = re.compile(r"silence_(start|end):\s*(-?[\d.]+)")

def _detect_silences(audio_path):
    """Run ffmpeg's silencedetect over the whole file once; return a sorted list
    of (start, end) gaps. Best-effort: returns [] if ffmpeg or the file is unavailable,
    so callers just fall back to word-boundary snapping."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio_path),
             "-af", f"silencedetect=noise={SILENCE_NOISE_DB}:d={SILENCE_MIN_DUR}",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("silencedetect failed to run: %s", e)
        return []
    gaps, pending_start = [], None
    for kind, val in _SILENCE_RE.findall(proc.stderr):
        t = float(val)
        if kind == "start":
            pending_start = t
        elif kind == "end" and pending_start is not None:
            gaps.append((pending_start, t))
            pending_start = None
    return gaps

def _snap_to_silence(gaps, t, upper, window=SILENCE_SEARCH_WINDOW):
    """Snap a cut boundary to a real silence gap near t, or None if there isn't one.

    Direction matters. Snapping the wrong way is what leaves ad audio in the cut:
    an ad's START must never move later (that leaves the ad's opening words), and
    its END must never move earlier (that leaves the ad's tail - the bug that let
    a sponsor's closing URL survive). So we only consider gaps that widen the cut:
      - start (upper=False): gaps at or before t, snapping back to the gap's END
        (the last silent instant before speech resumes).
      - end   (upper=True):  gaps at or after t, snapping forward to the gap's
        START (the first silent instant after speech stops).
    The returned point itself must lie within `window` of t, so a long gap can't
    drag the cut far from the intended boundary.
    """
    best, best_dist = None, window
    for a, b in gaps:
        if not upper:
            if a > t:        # gap lies entirely after a start boundary - wrong side
                continue
            point = min(b, t)
        else:
            if b < t:        # gap lies entirely before an end boundary - wrong side
                continue
            point = max(a, t)
        dist = abs(point - t)
        if dist < best_dist:
            best, best_dist = point, dist
    return best

def _postprocess(raw_segments, words, duration, audio_path=None):
    """Snap LLM times to word boundaries, then to a real silence gap in the audio
    when one is nearby (more precise than the LLM's guess or a fixed buffer),
    else fall back to a small fixed buffer. Clamp and merge overlaps."""
    if not words:
        return []
    starts = [w["start"] for w in words]
    ends = [w["end"] for w in words]
    # Only worth a full decode pass if the model actually returned something.
    gaps = _detect_silences(audio_path) if (audio_path and raw_segments) else []
    segs = []
    for r in raw_segments:
        try:
            s = float(r["start_sec"]); e = float(r["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if e <= s:
            continue
        s = _snap(words, starts, ends, s, upper=False)
        e = _snap(words, starts, ends, e, upper=True)
        snapped_s = _snap_to_silence(gaps, s, upper=False)
        snapped_e = _snap_to_silence(gaps, e, upper=True)
        s = snapped_s if snapped_s is not None else max(0.0, s - AD_BUFFER_SECONDS)
        e = snapped_e if snapped_e is not None else e + AD_BUFFER_SECONDS
        s = max(0.0, s)
        if duration:
            e = min(duration, e)
        # A degenerate span would silently cut nothing (or, if inverted, duplicate
        # audio downstream in build_keep_segments), so drop it rather than emit it.
        if e - s < MIN_SEGMENT_SECONDS:
            log.warning("Dropping degenerate ad segment [%.2f, %.2f] (%s)",
                        s, e, str(r.get("reason", "ad"))[:60])
            continue
        segs.append({"start": round(s, 2), "end": round(e, 2),
                     "reasons": [str(r.get("reason", "ad"))[:80]], "approved": True})
    segs.sort(key=lambda x: x["start"])
    merged = []
    for seg in segs:
        if merged and seg["start"] - merged[-1]["end"] < AD_MIN_GAP_SECONDS:
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
            for rr in seg["reasons"]:
                if rr not in merged[-1]["reasons"]:
                    merged[-1]["reasons"].append(rr)
        else:
            merged.append(seg)
    return merged

def _refine_with_silence(segs, audio_path, duration):
    """Widen already-built segments (the regex paths, which only ever apply a
    fixed buffer) out to nearby silence gaps, so a fallback cut lands as cleanly
    as an LLM one. Directional, so boundaries only ever move outward."""
    if not segs or not audio_path:
        return segs
    gaps = _detect_silences(audio_path)
    if not gaps:
        return segs
    for seg in segs:
        s2 = _snap_to_silence(gaps, seg["start"], upper=False)
        e2 = _snap_to_silence(gaps, seg["end"], upper=True)
        if s2 is not None:
            seg["start"] = round(max(0.0, s2), 2)
        if e2 is not None:
            seg["end"] = round(min(duration, e2) if duration else e2, 2)
    return segs

def _fingerprint_pass(audio_path):
    """Run audio-fingerprint matching against the known-ad library. Isolated
    in its own try/except and db session so a fingerprinting problem never
    blocks transcript-based detection or publishing."""
    if not FINGERPRINT_ENABLED or not audio_path:
        return []
    try:
        from app.database import get_db
        from app.fingerprint import match_library
        with get_db() as db:
            return match_library(db, audio_path)
    except Exception as e:
        log.warning("Fingerprint matching failed for %s: %s", audio_path, e)
        return []

def _merge_segments(*segment_lists):
    """Merge multiple already-individually-merged segment lists into one,
    combining anything that now overlaps or sits within AD_MIN_GAP_SECONDS."""
    all_segs = [dict(s, reasons=list(s["reasons"])) for lst in segment_lists for s in lst]
    all_segs.sort(key=lambda s: s["start"])
    merged = []
    for seg in all_segs:
        if merged and seg["start"] - merged[-1]["end"] < AD_MIN_GAP_SECONDS:
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
            for r in seg["reasons"]:
                if r not in merged[-1]["reasons"]:
                    merged[-1]["reasons"].append(r)
        else:
            merged.append(seg)
    return merged

def _call_anthropic(prompt_text):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=LLM_MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt_text}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    # Be robust to any stray prose around the JSON object.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model response")
    data = json.loads(text[start:end + 1])
    return data.get("segments", [])

def detect_ads(transcript_path, audio_path=None):
    """Return (segments, detector_label). audio_path (the raw episode mp3), if
    given, is used two ways: boundaries snap to real silence gaps instead of
    just a fixed buffer, and it's scanned against the fingerprint library for
    already-known ad clips before the transcript-based pass runs."""
    fp_segs = _fingerprint_pass(audio_path)
    fp_label = f"fingerprint({len(fp_segs)})+" if fp_segs else ""

    words, duration = _load_words(transcript_path)
    if not words:
        if fp_segs:
            return fp_segs, f"fingerprint({len(fp_segs)})"
        return [], "none(empty transcript)"

    def _regex(label):
        segs = _refine_with_silence(detect_ad_segments(Path(transcript_path)),
                                    audio_path, duration)
        return _merge_segments(fp_segs, segs), f"{fp_label}{label}"

    use_llm = LLM_PROVIDER == "anthropic"
    if use_llm and not ANTHROPIC_API_KEY:
        return _regex("regex(fallback: no API key)")
    if not use_llm:
        return _regex("regex")

    try:
        raw = _call_anthropic(_build_prompt(words))
        segs = _postprocess(raw, words, duration, audio_path)
        log.info("LLM (%s) detected %d ad segments", _short_model(), len(segs))
        return _merge_segments(fp_segs, segs), f"{fp_label}llm({_short_model()})"
    except Exception as e:
        log.error("LLM detection failed (%s); using regex fallback", e)
        return _regex(f"regex(fallback: {type(e).__name__})")
