"""Ad detection dispatcher.

detect_ads() returns (segments, detector_label). It uses the LLM provider when
configured and reachable, and automatically falls back to the regex detector on
any error or missing key, so publishing is never blocked. The label records
exactly which path ran (e.g. "llm(haiku-4.5)", "regex(fallback: no API key)").
"""
import json, logging
from bisect import bisect_right, bisect_left
from pathlib import Path
from app.config import (
    LLM_PROVIDER, ANTHROPIC_API_KEY, LLM_MODEL, AD_BUFFER_SECONDS, AD_MIN_GAP_SECONDS,
)
from app.detector import detect_ad_segments  # regex fallback

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You identify advertising in a podcast transcript. The transcript is given as "
    "lines each prefixed with a start time in seconds, e.g. `[123.4] words...`.\n\n"
    "Mark every ADVERTISING / SPONSORSHIP segment: host-read ads, dynamically "
    "inserted ads, 'brought to you by', 'support for this show comes from', promo "
    "codes, discount offers, and calls to visit a sponsor's website. Include the "
    "FULL span of each ad read, from the first sponsor word to the last, including "
    "'we'll be right back' lead-ins and the trailing legal/terms lines.\n\n"
    "Do NOT mark editorial content, the hosts merely mentioning a company while "
    "discussing news, the show intro/outro, credits, or listener housekeeping "
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

def _postprocess(raw_segments, words, duration):
    """Snap LLM times to word boundaries, add a small buffer, clamp and merge."""
    if not words:
        return []
    starts = [w["start"] for w in words]
    ends = [w["end"] for w in words]
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
        s = max(0.0, s - AD_BUFFER_SECONDS)
        e = e + AD_BUFFER_SECONDS
        if duration:
            e = min(duration, e)
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

def detect_ads(transcript_path):
    """Return (segments, detector_label)."""
    words, duration = _load_words(transcript_path)
    if not words:
        return [], "none(empty transcript)"

    use_llm = LLM_PROVIDER == "anthropic"
    if use_llm and not ANTHROPIC_API_KEY:
        segs = detect_ad_segments(Path(transcript_path))
        return segs, "regex(fallback: no API key)"
    if not use_llm:
        segs = detect_ad_segments(Path(transcript_path))
        return segs, "regex"

    try:
        raw = _call_anthropic(_build_prompt(words))
        segs = _postprocess(raw, words, duration)
        log.info("LLM (%s) detected %d ad segments", _short_model(), len(segs))
        return segs, f"llm({_short_model()})"
    except Exception as e:
        log.error("LLM detection failed (%s); using regex fallback", e)
        segs = detect_ad_segments(Path(transcript_path))
        return segs, f"regex(fallback: {type(e).__name__})"
