import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
AUDIO_DIR = DATA_DIR / "audio"
# Artwork uploads and human-readable processing logs live on the NAS mount
# (AUDIO_DIR is the NAS-backed volume), so they survive container rebuilds.
ARTWORK_DIR = AUDIO_DIR / "artwork"
LOG_DIR = AUDIO_DIR / "logs"
PROCESSING_LOG_FILE = LOG_DIR / "processing.log"
DB_PATH = DATA_DIR / "pivot.db"

PIVOT_RSS = "https://feeds.megaphone.fm/pivot"
POLL_INTERVAL_HOURS = int(os.getenv("POLL_INTERVAL_HOURS", "1"))

# --- LLM ad detection (pluggable) ---------------------------------------
# Provider for ad detection: "anthropic" (Haiku via API), "local" (reserved
# for a future Ollama/llama.cpp backend), or "none" to force the regex path.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5")
# Base URL for a future local/OpenAI-compatible endpoint (unused by anthropic).
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()

# --- Audio fingerprint ad detection (pluggable, runs alongside LLM/regex) --
# Matches episode audio against a library of previously-confirmed ad clips
# using Chromaprint. Catches reused/programmatic ad reads by audio alone, no
# transcript or API call needed once a clip is known. The library grows
# automatically: every human-approved cut gets fingerprinted after publish.
FINGERPRINT_ENABLED = os.getenv("FINGERPRINT_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
# Fraction of mismatched bits (0-1) below which a candidate counts as a match.
# Real-audio testing: true matches land around 0.04-0.12 bit-error even
# across an MP3 lossy round-trip; unrelated audio sits at 0.35+. 0.20 leaves
# a wide margin on both sides.
FINGERPRINT_MATCH_THRESHOLD = float(os.getenv("FINGERPRINT_MATCH_THRESHOLD", "0.20"))
# Sample rate used to decode audio before fingerprinting. Chromaprint
# resamples internally regardless, so a lower rate just means less audio for
# ffmpeg to pipe through - doesn't affect match quality.
FINGERPRINT_SAMPLE_RATE = int(os.getenv("FINGERPRINT_SAMPLE_RATE", "11025"))

# Delete published episodes this many days after they are cut/published
# (0 = keep forever). Distinct from MAX_EPISODE_AGE_DAYS, which is the
# download window for new episodes.
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30") or 0)
# Timezone for the fast-poll release window (Pivot releases on Eastern time)
SCHEDULE_TZ = os.getenv("SCHEDULE_TZ", "America/New_York")

# Auto-cut and publish as soon as an episode is transcribed, without waiting for
# manual review. You can still open a published episode and re-edit/re-publish.
AUTO_PUBLISH = os.getenv("AUTO_PUBLISH", "true").strip().lower() in ("1", "true", "yes", "on")

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")
# Cap CPU threads so transcription doesn't starve the host (0 = ctranslate2 default, all cores)
WHISPER_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "2"))

AD_BUFFER_SECONDS = float(os.getenv("AD_BUFFER_SECONDS", "3.0"))
AD_MIN_GAP_SECONDS = float(os.getenv("AD_MIN_GAP_SECONDS", "30.0"))
# How far past an ad block to look for a "we're back" style resume phrase
AD_RESUME_WINDOW_SECONDS = float(os.getenv("AD_RESUME_WINDOW_SECONDS", "90.0"))

# Set to 0 to disable age filter (download all episodes)
_age = os.getenv("MAX_EPISODE_AGE_DAYS", "30")
MAX_EPISODE_AGE_DAYS = int(_age) if _age else 0

# Sent on every RSS/audio download so requests look like a normal Apple
# Podcasts client rather than a script. Some feeds/CDNs behind dynamic ad
# insertion vary what they serve (or block outright) based on user-agent, so
# blending in avoids being singled out.
DOWNLOAD_USER_AGENT = os.getenv(
    "DOWNLOAD_USER_AGENT",
    "Podcasts/4025.610.1 CFNetwork/1408.0.4 Darwin/22.5.0",
)

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
FEED_TITLE = "Pivot (Ad-Free)"
FEED_DESCRIPTION = "Pivot with Kara Swisher and Scott Galloway - ads removed."
FEED_AUTHOR = "Kara Swisher & Scott Galloway"
# Podcast artwork + metadata (required by Pocket Casts/Apple to load the show)
FEED_IMAGE = os.getenv("FEED_IMAGE", "https://megaphone.imgix.net/podcasts/d6280242-e5c9-11e8-a7e3-d766bb7d2d3e/image/bbb7849ef30865b87218347b3a090613.png?ixlib=rails-4.3.1&max-w=3000&max-h=3000&fit=crop&auto=format,compress")
FEED_CATEGORY = os.getenv("FEED_CATEGORY", "News")
FEED_OWNER_EMAIL = os.getenv("FEED_OWNER_EMAIL", "podcast@chmbrs.dev")
# Secret token required as a ?t=... query param on the public feed and audio
# URLs. Unlike Basic Auth (which Pocket Casts won't send to enclosures), query
# tokens are preserved, so playback works. Leave empty to disable. Use a
# URL-safe value (e.g. secrets.token_urlsafe).
FEED_TOKEN = os.getenv("FEED_TOKEN", "")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme-in-production")
