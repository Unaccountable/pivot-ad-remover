import json
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from app.config import BASE_URL, FEED_TITLE, FEED_DESCRIPTION, FEED_AUTHOR
from app.database import get_db

def _dur(secs):
    if not secs: return "0:00:00"
    return f"{int(secs//3600)}:{int((secs%3600)//60):02d}:{int(secs%60):02d}"

def generate_feed():
    with get_db() as db:
        rows = db.execute("SELECT * FROM episodes WHERE status='published' ORDER BY pub_date DESC LIMIT 50").fetchall()
    items = []
    for row in rows:
        ep = dict(row)
        url = f"{BASE_URL}/audio/{Path(ep['clean_audio_path']).name}"
        try: size = Path(ep["clean_audio_path"]).stat().st_size
        except: size = 0
        pub = ep.get("pub_date") or datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        items.append(f"""
    <item>
      <title>{escape(ep['title'])}</title>
      <description><![CDATA[{ep.get('description','')}]]></description>
      <pubDate>{pub}</pubDate>
      <enclosure url="{url}" length="{size}" type="audio/mpeg"/>
      <guid isPermaLink="false">{escape(ep['guid'])}-clean</guid>
      <itunes:duration>{_dur(ep.get('duration_secs'))}</itunes:duration>
      <itunes:author>{escape(FEED_AUTHOR)}</itunes:author>
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(FEED_TITLE)}</title>
    <description>{escape(FEED_DESCRIPTION)}</description>
    <link>{BASE_URL}</link>
    <language>en-us</language>
    <itunes:author>{escape(FEED_AUTHOR)}</itunes:author>
    <itunes:explicit>no</itunes:explicit>
    <atom:link xmlns:atom="http://www.w3.org/2005/Atom" href="{BASE_URL}/feed.xml" rel="self" type="application/rss+xml"/>
    {'\n'.join(items)}
  </channel>
</rss>"""
