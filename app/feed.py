import json
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from app.config import BASE_URL, FEED_TITLE, FEED_DESCRIPTION, FEED_AUTHOR, FEED_IMAGE, FEED_CATEGORY, FEED_OWNER_EMAIL, FEED_TOKEN
from app.database import get_db

def _dur(secs):
    if not secs: return "0:00:00"
    return f"{int(secs//3600)}:{int((secs%3600)//60):02d}:{int(secs%60):02d}"

def _token_qs():
    """Query string carrying the access token for audio URLs (empty if disabled)."""
    return f"?t={FEED_TOKEN}" if FEED_TOKEN else ""

def generate_feed():
    with get_db() as db:
        rows = db.execute("SELECT * FROM episodes WHERE status='published' ORDER BY pub_date DESC LIMIT 50").fetchall()
    items = []
    for row in rows:
        ep = dict(row)
        audio_url = f"{BASE_URL}/audio/{Path(ep['clean_audio_path']).name}{_token_qs()}"
        try: size = Path(ep["clean_audio_path"]).stat().st_size
        except: size = 0
        duration = _dur(ep.get("duration_secs"))
        pub = ep.get("pub_date") or datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        item = (
            "\n    <item>"
            f"\n      <title>{escape(ep['title'])}</title>"
            f"\n      <description><![CDATA[{ep.get('description', '')}]]></description>"
            f"\n      <pubDate>{pub}</pubDate>"
            f"\n      <enclosure url=\"{escape(audio_url)}\" length=\"{size}\" type=\"audio/mpeg\"/>"
            f"\n      <guid isPermaLink=\"false\">{escape(ep['guid'])}-clean</guid>"
            f"\n      <itunes:duration>{duration}</itunes:duration>"
            f"\n      <itunes:author>{escape(FEED_AUTHOR)}</itunes:author>"
            "\n    </item>"
        )
        items.append(item)
    items_str = "".join(items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">\n'
        '  <channel>\n'
        f'  <title>{escape(FEED_TITLE)}</title>\n'
        f'  <description>{escape(FEED_DESCRIPTION)}</description>\n'
        f'  <link>{BASE_URL}</link>\n'
        '  <language>en-us</language>\n'
        f'  <itunes:author>{escape(FEED_AUTHOR)}</itunes:author>\n'
        '  <itunes:explicit>no</itunes:explicit>\n'
        '  <itunes:type>episodic</itunes:type>\n'
        f'  <itunes:image href="{escape(FEED_IMAGE)}"/>\n'
        f'  <image><url>{escape(FEED_IMAGE)}</url><title>{escape(FEED_TITLE)}</title><link>{BASE_URL}</link></image>\n'
        f'  <itunes:category text="{escape(FEED_CATEGORY)}"/>\n'
        f'  <itunes:owner><itunes:name>{escape(FEED_AUTHOR)}</itunes:name><itunes:email>{escape(FEED_OWNER_EMAIL)}</itunes:email></itunes:owner>\n'
        f'  <itunes:summary>{escape(FEED_DESCRIPTION)}</itunes:summary>\n'
        f'  <atom:link xmlns:atom="http://www.w3.org/2005/Atom" href="{BASE_URL}/feed.xml" rel="self" type="application/rss+xml"/>\n'
        + items_str +
        '\n  </channel>\n'
        '</rss>'
    )
