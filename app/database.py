import sqlite3, json, re
from contextlib import contextmanager
from app.config import (
    DB_PATH, PIVOT_RSS, FEED_TOKEN, FEED_TITLE, FEED_DESCRIPTION, FEED_AUTHOR,
    FEED_IMAGE, FEED_CATEGORY, FEED_OWNER_EMAIL,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS podcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    source_rss TEXT NOT NULL,
    feed_token TEXT NOT NULL,
    title TEXT, description TEXT, author TEXT,
    category TEXT, owner_email TEXT,
    image_url TEXT,            -- external/source artwork URL
    image_file TEXT,           -- uploaded artwork filename on the NAS (overrides image_url)
    fast_weekdays TEXT,        -- csv of weekday ints, Mon=0 (e.g. "1,4" = Tue,Fri)
    fast_hour_start INTEGER,   -- local-time hour the fast-poll window opens
    fast_hour_end INTEGER,     -- local-time hour the fast-poll window closes
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    podcast_id INTEGER,
    guid TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT,
    pub_date TEXT, original_url TEXT, duration_secs REAL,
    raw_audio_path TEXT, clean_audio_path TEXT, transcript_path TEXT,
    ad_segments TEXT, detector TEXT, status TEXT NOT NULL DEFAULT 'pending',
    error_msg TEXT, published_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS processing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now')),
    podcast_id INTEGER, podcast_name TEXT, episode_title TEXT,
    status TEXT,               -- published | failed
    detector TEXT,             -- e.g. llm(haiku-4.5), regex(fallback: no API key)
    segments INTEGER, cut_secs REAL, final_secs REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ad_fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT,
    podcast_id INTEGER,
    episode_id INTEGER,
    fp_json TEXT NOT NULL,      -- JSON array of Chromaprint raw fingerprint ints
    fp_len INTEGER NOT NULL,
    duration_secs REAL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    last_matched_at TEXT
);
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

def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "podcast"

def _columns(db, table):
    return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}

def _add_column_if_missing(db, table, col, decl):
    if col not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

def _migrate(db):
    """Bring an existing (pre-multipodcast) DB up to the current schema and seed
    the Pivot podcast, preserving its existing feed token so the live
    subscription keeps working."""
    # Columns added to episodes after the original single-feed schema.
    _add_column_if_missing(db, "episodes", "podcast_id", "INTEGER")
    _add_column_if_missing(db, "episodes", "detector", "TEXT")
    _add_column_if_missing(db, "episodes", "published_at", "TEXT")

    # Seed the Pivot podcast once, reusing the existing FEED_TOKEN.
    existing = db.execute("SELECT id FROM podcasts WHERE slug='pivot'").fetchone()
    if not existing:
        db.execute(
            """INSERT INTO podcasts
               (slug,name,source_rss,feed_token,title,description,author,category,
                owner_email,image_url,fast_weekdays,fast_hour_start,fast_hour_end,active)
               VALUES ('pivot',?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (FEED_TITLE, PIVOT_RSS, FEED_TOKEN, FEED_TITLE, FEED_DESCRIPTION,
             FEED_AUTHOR, FEED_CATEGORY, FEED_OWNER_EMAIL, FEED_IMAGE, "1,4", 5, 8),
        )
    pivot_id = db.execute("SELECT id FROM podcasts WHERE slug='pivot'").fetchone()["id"]
    # Backfill any episodes that predate the podcast_id column.
    db.execute("UPDATE episodes SET podcast_id=? WHERE podcast_id IS NULL", (pivot_id,))

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
        _migrate(db)

# --- podcasts -----------------------------------------------------------
def _row(row):
    return dict(row) if row else None

def get_podcast(db, podcast_id):
    return _row(db.execute("SELECT * FROM podcasts WHERE id=?", (podcast_id,)).fetchone())

def get_podcast_by_slug(db, slug):
    return _row(db.execute("SELECT * FROM podcasts WHERE slug=?", (slug,)).fetchone())

def list_podcasts(db, active_only=False):
    q = "SELECT * FROM podcasts"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY name"
    return [dict(r) for r in db.execute(q).fetchall()]

# --- episodes -----------------------------------------------------------
def get_episode(db, episode_id):
    row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if row:
        d = dict(row)
        if d["ad_segments"]:
            d["ad_segments"] = json.loads(d["ad_segments"])
        return d
    return None

def get_episode_by_clean_name(db, filename):
    from app.config import AUDIO_DIR
    full = str(AUDIO_DIR / "clean" / filename)
    row = db.execute("SELECT * FROM episodes WHERE clean_audio_path=?", (full,)).fetchone()
    return dict(row) if row else None

def set_status(db, episode_id, status, error_msg=None):
    db.execute(
        "UPDATE episodes SET status=?, error_msg=?, updated_at=datetime('now') WHERE id=?",
        (status, error_msg, episode_id),
    )

# --- ad fingerprint library -----------------------------------------------
def add_fingerprint(db, label, fp, duration_secs, podcast_id=None, episode_id=None):
    """Store a Chromaprint raw fingerprint (list of ints) for a confirmed ad clip."""
    cur = db.execute(
        "INSERT INTO ad_fingerprints (label,podcast_id,episode_id,fp_json,fp_len,duration_secs) "
        "VALUES (?,?,?,?,?,?)",
        (label, podcast_id, episode_id, json.dumps(fp), len(fp), duration_secs),
    )
    return cur.lastrowid

def list_fingerprints(db):
    return [dict(r) for r in db.execute(
        "SELECT * FROM ad_fingerprints ORDER BY hit_count DESC, id DESC"
    ).fetchall()]

def record_fingerprint_hit(db, fp_id):
    db.execute(
        "UPDATE ad_fingerprints SET hit_count=hit_count+1, last_matched_at=datetime('now') WHERE id=?",
        (fp_id,),
    )

def delete_fingerprint(db, fp_id):
    db.execute("DELETE FROM ad_fingerprints WHERE id=?", (fp_id,))
