import json, logging, re
from bisect import bisect_right
from pathlib import Path
from app.config import AD_BUFFER_SECONDS, AD_MIN_GAP_SECONDS, AD_RESUME_WINDOW_SECONDS

log = logging.getLogger(__name__)
AD_PATTERNS = [
    r"support for (this show|the show|today's show|pivot) comes from",
    r"this (episode|show|week) is (brought to you|sponsored) by",
    r"our (sponsor|presenting sponsor|partner)s?\b",
    r"(brought to you|sponsored|presented) by",
    r"go to [a-z0-9\-]+\.com\s*(slash|/)\s*\w+",
    r"use (promo )?code\s+\w{3,}",
    r"(visit|check out|head to|go to) [a-z0-9\-]+\.(com|org|net|co)\b",
    r"(get|save|receive|take) (up to )?\d{1,2}% off",
    r"\$\d+ off",
    r"(free trial|free shipping|limited time|money.back guarantee)",
    r"terms (and conditions )?apply",
    r"(download|sign up|subscribe).{0,30}(today|now|free)",
    r"(and now,? )?a (quick )?word from",
    r"we('ll| will) be (right )?back",
    r"(take|time for) a (quick|short) break",
    r"stay with us",
    r"before we (get started|continue|dive in)",
    r"thanks? to (our|this week's?) (sponsors?|partners?)",
]

# Phrases spoken when real content starts/resumes. These are NOT cut themselves;
# they anchor ad-block boundaries: the show intro marks the end of pre-roll ads,
# resume phrases mark the end of a mid-roll break.
INTRO_PATTERN = r"this is pivot from"
RESUME_PATTERNS = [
    INTRO_PATTERN,
    r"welcome back\b",
    r"(scott|kara)\W{1,4}we('| a)re back",
    r"(and|okay|ok)\W{1,4}we('| a)re back",
    r"we('| a)re back\W{1,4}(scott|kara)\b",
]

INTRO_SEARCH_WINDOW = 300.0   # intro must appear this early to imply a pre-roll block
MIN_PREROLL_SECONDS = 3.0     # ignore trivial pre-roll gaps
RESUME_LEAD_SECONDS = 0.3     # end cuts just before the resume phrase so it stays audible

_compiled_ads = [re.compile(p, re.IGNORECASE) for p in AD_PATTERNS]
_compiled_resume = [re.compile(p, re.IGNORECASE) for p in RESUME_PATTERNS]

def _build_text(words):
    """Join word tokens into one normalized string, tracking each word's char offset.

    faster-whisper word tokens carry leading whitespace, so they must be
    stripped before joining or multi-word patterns never match.
    """
    parts, offsets, pos = [], [], 0
    for w in words:
        token = w["word"].strip().replace("’", "'")
        offsets.append(pos)
        parts.append(token)
        pos += len(token) + 1
    return " ".join(parts), offsets

def _find_hits(compiled, text, offsets, words):
    hits = []
    for pattern in compiled:
        for m in pattern.finditer(text):
            i = bisect_right(offsets, m.start()) - 1
            j = bisect_right(offsets, max(m.start(), m.end() - 1)) - 1
            hits.append((words[i]["start"], words[j]["end"], pattern.pattern))
    hits.sort(key=lambda h: h[0])
    return hits

def detect_ad_segments(transcript_path):
    data = json.loads(transcript_path.read_text())
    words, duration = data["words"], data.get("duration") or 0
    if not words: return []
    text, offsets = _build_text(words)
    ad_hits = _find_hits(_compiled_ads, text, offsets, words)
    resume_hits = _find_hits(_compiled_resume, text, offsets, words)

    # Merge nearby pattern hits into ad blocks.
    segments = []
    for s, e, reason in ad_hits:
        start = max(0.0, s - AD_BUFFER_SECONDS)
        end = e + AD_BUFFER_SECONDS
        if duration: end = min(duration, end)
        if segments and (start - segments[-1]["end"]) < AD_MIN_GAP_SECONDS:
            segments[-1]["end"] = max(segments[-1]["end"], end)
            if reason not in segments[-1]["reasons"]:
                segments[-1]["reasons"].append(reason)
        else:
            segments.append({"start": start, "end": end, "reasons": [reason], "approved": True})

    # Extend each block forward to a resume phrase spoken shortly after it, so
    # trailing ad copy that matched no pattern still gets cut. One extension per
    # block, and the phrase itself is kept in the audio.
    for seg in segments:
        for rs, _, rreason in resume_hits:
            target = rs - RESUME_LEAD_SECONDS
            if seg["end"] < target <= seg["end"] + AD_RESUME_WINDOW_SECONDS:
                seg["end"] = min(target, duration) if duration else target
                seg["reasons"].append("cut to resume: " + rreason)
                break

    # Pre-roll: the show intro appearing early means everything before it is ads.
    intro = next((h for h in resume_hits if h[2] == INTRO_PATTERN and h[0] <= INTRO_SEARCH_WINDOW), None)
    if intro and intro[0] - RESUME_LEAD_SECONDS >= MIN_PREROLL_SECONDS:
        segments.insert(0, {"start": 0.0, "end": intro[0] - RESUME_LEAD_SECONDS,
                            "reasons": ["pre-roll before show intro"], "approved": True})

    # Final pass: merge anything that now overlaps.
    segments.sort(key=lambda x: x["start"])
    merged = []
    for seg in segments:
        if merged and seg["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
            for r in seg["reasons"]:
                if r not in merged[-1]["reasons"]:
                    merged[-1]["reasons"].append(r)
        else:
            merged.append(seg)
    log.info("Detected %d ad segments (%d pattern hits, %d resume markers)",
             len(merged), len(ad_hits), len(resume_hits))
    return merged
