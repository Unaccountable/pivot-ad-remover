# Pivot Ad-Free

Self-hosted Docker app: polls one or more podcast RSS feeds, transcribes new episodes with faster-whisper, detects ads (Claude Haiku by default, regex fallback), cuts them out with ffmpeg, and republishes a clean RSS feed per show for Pocket Casts/Apple Podcasts.

## Architecture
```
RSS Poll → Download MP3 → faster-whisper transcribe → LLM (or regex) ad detection → ffmpeg cut → RSS published
```
`AUTO_PUBLISH=true` (default) skips manual review — episodes cut and publish automatically as soon as they're transcribed. Published episodes stay editable: re-saving one re-cuts and republishes it, and a **Re-detect Ads** button re-scans the existing transcript without re-transcribing.

## Multi-Podcast
The app is not limited to a single show. Add podcasts from the admin UI (**Podcasts** page) by pasting their source RSS URL — title, artwork, author, and category are pulled automatically. Each podcast gets:
- Its own clean feed at `/feed/<slug>.xml` (the original seeded podcast keeps the legacy path `/feed.xml`)
- Its own `feed_token` (`?t=...` query param) gating both the feed and its audio files
- Optional custom artwork upload, and an optional fast-poll window (see below)

Episodes across all podcasts share the same transcription/detection queue, processed one at a time — a slow box means shows queue up behind each other.

## Ad Detection
`LLM_PROVIDER=anthropic` (default) sends the timestamped transcript to Claude Haiku (`LLM_MODEL=claude-haiku-4-5`) and snaps the returned ad boundaries to Whisper word timestamps (~2-3¢/episode). Requires a pay-as-you-go key in `ANTHROPIC_API_KEY` (a Pro/Max subscription won't work here — this calls the API directly). Without a key, or with `LLM_PROVIDER=none`, it falls back to a regex pattern matcher (`app/detector.py`).

## Polling Schedule
- **Normally:** every `POLL_INTERVAL_HOURS` (default 1)
- **Per-podcast fast-poll window:** optionally configure a show to poll every 3 minutes during its release window (e.g. Tue/Fri 5-8am ET for a show that drops on a fixed schedule), via `SCHEDULE_TZ`

## Episode Retention
Only episodes newer than `MAX_EPISODE_AGE_DAYS` (default 30) are downloaded; older ones (audio, transcript, DB row) are skipped/cleaned automatically. Separately, `RETENTION_DAYS` (default 30) deletes *published* episodes that many days after publish, to keep storage bounded. Set either to `0` to disable.

## Admin UI
LAN-only, served on the app port (see Setup). Includes:
- **Podcasts** — add/edit/delete shows, rotate feed tokens, upload artwork
- **Episodes** — filterable by podcast, manual re-detect/re-publish
- **Logs** — searchable processing log (every cut/publish, detector used, ad segment count)

## Setup

### 1. Create a folder for audio on your NAS/storage
This is mounted read/write into the container and holds raw + clean audio, artwork uploads, and processing logs for **all** podcasts (not per-show).

### 2. Mount it on the Docker host
```bash
sudo apt install cifs-utils   # example: SMB/CIFS mount
sudo mkdir -p /mnt/truenas
sudo mount -t cifs //<nas-ip>/<share> /mnt/truenas -o username=<user>,password=<pass>,uid=1000,gid=1000
# Add to /etc/fstab for persistence
```

### 3. Set timezone
Match `SCHEDULE_TZ` if you're using a fast-poll window:
```bash
sudo timedatectl set-timezone America/New_York
```

### 4. Configure
```bash
cp .env.example .env
nano .env  # set BASE_URL, AUDIO_MOUNT, ANTHROPIC_API_KEY, SECRET_KEY, etc.
```

### 5. Build & run
```bash
docker compose up -d --build
# First build ~5-10min (downloads the Whisper model)
```

### 6. Add your first podcast
Open the admin UI (`http://<host>:8000`) → **Podcasts** → add by RSS URL. Or seed/poll the default show via:
```bash
curl -X POST http://localhost:8000/admin/poll
```

### 7. Subscribe
Pocket Casts → Add Podcast → Add URL → `http://<your-url>/feed.xml?t=<feed_token>` (or `/feed/<slug>.xml?t=...` for additional shows).

## Re-running Ad Detection
Open an episode's review page and click **Re-detect Ads**, or via curl:
```bash
curl -X POST http://localhost:8000/admin/redetect/<episode_id>
```

## Useful Commands
```bash
docker compose logs -f
curl -X POST http://localhost:8000/admin/poll
docker compose restart
docker compose up -d --build  # after code changes
```

## Resource Notes
Transcription is CPU-bound and single-threaded through the queue — a low-core host will process episodes serially and can fall behind on release days. `cpus`/`mem_limit` in `docker-compose.yml` (via `CPU_LIMIT`/`MEM_LIMIT` in `.env`) cap the container so transcription doesn't starve the rest of the host. Switching `WHISPER_MODEL` from `medium` to `small` is roughly 3x faster on CPU with a modest accuracy tradeoff.
