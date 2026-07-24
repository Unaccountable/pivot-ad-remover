import json
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from app.config import BASE_URL, ARTWORK_DIR
from app.database import get_db

def _dur(secs):
    if not secs: return "0:00:00"
    return f"{int(secs//3600)}:{int((secs%3600)//60):02d}:{int(secs%60):02d}"

def _feed_path(pod):
    """Public path for a podcast's feed. Pivot keeps the legacy /feed.xml."""
    return "/feed.xml" if pod["slug"] == "pivot" else f"/feed/{pod['slug']}.xml"

def _image_url(pod):
    """Uploaded artwork on the NAS overrides the source feed's image. A ?v=<mtime>
    cache-buster makes the URL change on each re-upload so podcast apps (which
    cache cover art by URL) re-fetch the new image."""
    if pod.get("image_file"):
        try:
            v = int((ARTWORK_DIR / pod["image_file"]).stat().st_mtime)
        except OSError:
            v = 0
        return f"{BASE_URL}/artwork/{pod['slug']}?v={v}"
    return pod.get("image_url") or ""

def generate_feed(pod):
    """Render the RSS feed for a single podcast (a podcasts-table row dict)."""
    tok = f"?t={pod['feed_token']}" if pod.get("feed_token") else ""
    title = pod.get("title") or pod["name"]
    author = pod.get("author") or ""
    desc = pod.get("description") or ""
    category = pod.get("category") or "News"
    owner_email = pod.get("owner_email") or ""
    image = _image_url(pod)
    self_href = f"{BASE_URL}{_feed_path(pod)}{tok}"

    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM episodes WHERE podcast_id=? AND status='published' "
            "ORDER BY pub_date DESC LIMIT 50", (pod["id"],)
        ).fetchall()

    items = []
    for row in rows:
        ep = dict(row)
        audio_url = f"{BASE_URL}/audio/{Path(ep['clean_audio_path']).name}{tok}"
        try: size = Path(ep["clean_audio_path"]).stat().st_size
        except Exception: size = 0
        duration = _dur(ep.get("duration_secs"))
        pub = ep.get("pub_date") or datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        items.append(
            "\n    <item>"
            f"\n      <title>{escape(ep['title'])}</title>"
            f"\n      <description><![CDATA[{ep.get('description', '')}]]></description>"
            f"\n      <pubDate>{pub}</pubDate>"
            f"\n      <enclosure url=\"{escape(audio_url)}\" length=\"{size}\" type=\"audio/mpeg\"/>"
            f"\n      <guid isPermaLink=\"false\">{escape(ep['guid'])}-clean</guid>"
            f"\n      <itunes:duration>{duration}</itunes:duration>"
            f"\n      <itunes:author>{escape(author)}</itunes:author>"
            "\n    </item>"
        )
    items_str = "".join(items)
    image_tags = ""
    if image:
        image_tags = (
            f'  <itunes:image href="{escape(image)}"/>\n'
            f'  <image><url>{escape(image)}</url><title>{escape(title)}</title><link>{BASE_URL}</link></image>\n'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">\n'
        '  <channel>\n'
        f'  <title>{escape(title)}</title>\n'
        f'  <description>{escape(desc)}</description>\n'
        f'  <link>{BASE_URL}</link>\n'
        '  <language>en-us</language>\n'
        f'  <itunes:author>{escape(author)}</itunes:author>\n'
        '  <itunes:explicit>no</itunes:explicit>\n'
        '  <itunes:type>episodic</itunes:type>\n'
        + image_tags +
        f'  <itunes:category text="{escape(category)}"/>\n'
        f'  <itunes:owner><itunes:name>{escape(author)}</itunes:name><itunes:email>{escape(owner_email)}</itunes:email></itunes:owner>\n'
        f'  <itunes:summary>{escape(desc)}</itunes:summary>\n'
        f'  <atom:link xmlns:atom="http://www.w3.org/2005/Atom" href="{escape(self_href)}" rel="self" type="application/rss+xml"/>\n'
        + items_str +
        '\n  </channel>\n'
        '</rss>'
    )
