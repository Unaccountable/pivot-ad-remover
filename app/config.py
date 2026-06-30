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

AD_BUFFER_SECONDS = float(os.getenv("AD_BUFFER_SECONDS", "3.0"))
AD_MIN_GAP_SECONDS = float(os.getenv("AD_MIN_GAP_SECONDS", "30.0"))

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
FEED_TITLE = "Pivot (Ad-Free)"
FEED_DESCRIPTION = "Pivot with Kara Swisher and Scott Galloway - ads removed."
FEED_AUTHOR = "Kara Swisher & Scott Galloway"
SECRET_KEY = os.getenv("SECRET_KEY", "changeme-in-production")
