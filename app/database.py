import sqlite3, json
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT,
    pub_date TEXT, original_url TEXT, duration_secs REAL,
    raw_audio_path TEXT, clean_audio_path TEXT, transcript_path TEXT,
    ad_segments TEXT, status TEXT NOT NULL DEFAULT 'pending',
    error_msg TEXT, created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

@contextmanager
def get_db():
    # timeout + WAL let the scheduler, transcriber, and web threads write
    # concurrently instead of raising "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)

def get_episode(db, episode_id):
    row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if row:
        d = dict(row)
        if d["ad_segments"]:
            d["ad_segments"] = json.loads(d["ad_segments"])
        return d
    return None

def set_status(db, episode_id, status, error_msg=None):
    db.execute(
        "UPDATE episodes SET status=?, error_msg=?, updated_at=datetime('now') WHERE id=?",
        (status, error_msg, episode_id),
    )
