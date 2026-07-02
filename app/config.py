import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "pivot.db"

PIVOT_RSS = "https://feeds.megaphone.fm/pivot"
POLL_INTERVAL_HOURS = int(os.getenv("POLL_INTERVAL_HOURS", "1"))

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

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
FEED_TITLE = "Pivot (Ad-Free)"
FEED_DESCRIPTION = "Pivot with Kara Swisher and Scott Galloway - ads removed."
FEED_AUTHOR = "Kara Swisher & Scott Galloway"
# Podcast artwork + metadata (required by Pocket Casts/Apple to load the show)
FEED_IMAGE = os.getenv("FEED_IMAGE", "https://megaphone.imgix.net/podcasts/d6280242-e5c9-11e8-a7e3-d766bb7d2d3e/image/bbb7849ef30865b87218347b3a090613.png?ixlib=rails-4.3.1&max-w=3000&max-h=3000&fit=crop&auto=format,compress")
FEED_CATEGORY = os.getenv("FEED_CATEGORY", "News")
FEED_OWNER_EMAIL = os.getenv("FEED_OWNER_EMAIL", "podcast@chmbrs.dev")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme-in-production")
