import json, logging, re
from pathlib import Path
from app.config import AD_BUFFER_SECONDS, AD_MIN_GAP_SECONDS

log = logging.getLogger(__name__)
AD_PATTERNS = [
    r"this (episode|show|week) is (brought to you|sponsored) by",
    r"our (sponsor|presenting sponsor|partner)s?\b",
    r"(brought to you|sponsored) by",
    r"go to [a-z0-9]+\.com\s*slash\s*pivot",
    r"use (code|promo( code)?)\s+(PIVOT|pivot)",
    r"(visit|check out|head to|go to) [a-z0-9\-]+\.com",
    r"(get|save|receive) \d{1,2}% off",
    r"(free trial|free shipping|limited time)",
    r"(download|sign up|subscribe).{0,30}(today|now|free)",
    r"(and|now) a word from",
    r"we'll be right back",
    r"welcome back (to pivot|everyone)",
    r"before we (get started|continue|dive in)",
    r"thanks? to (our|this week's?) (sponsors?|partners?)",
]
_compiled = [re.compile(p, re.IGNORECASE) for p in AD_PATTERNS]

def detect_ad_segments(transcript_path):
    data = json.loads(transcript_path.read_text())
    words, duration = data["words"], data.get("duration", 0)
    if not words: return []
    flagged = []
    for i in range(len(words)):
        chunk = words[i:i+20]
        text = " ".join(w["word"] for w in chunk)
        for p in _compiled:
            if p.search(text):
                flagged.append((chunk[0]["start"], chunk[-1]["end"], p.pattern))
                break
    if not flagged: return []
    segments = []
    for s, e, reason in flagged:
        start = max(0.0, s - AD_BUFFER_SECONDS)
        end = min(duration, e + AD_BUFFER_SECONDS)
        if segments and (start - segments[-1]["end"]) < AD_MIN_GAP_SECONDS:
            segments[-1]["end"] = max(segments[-1]["end"], end)
            segments[-1]["reasons"].append(reason)
        else:
            segments.append({"start": start, "end": end, "reasons": [reason], "approved": True})
    log.info("Detected %d ad segments", len(segments))
    return segments
