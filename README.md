# Pivot Ad-Free

Self-hosted Docker app: downloads Pivot episodes, transcribes with faster-whisper, detects ads, lets you review cuts, publishes clean RSS feed to Pocket Casts.

## Architecture
```
RSS Poll → Download MP3 → faster-whisper transcribe → Ad detection → [You Review] → ffmpeg cut → RSS published
```

## Polling Schedule
- **Normally:** every 1 hour
- **Tuesday & Friday 05:00–06:00 local time:** every 3 minutes (Pivot release window)

## Setup

### 1. Create TrueNAS folder
Ensure `SMB_Share/podcasts/pivot` exists on your TrueNAS (192.168.0.144).

### 2. Mount on Ubuntu VM
```bash
sudo apt install cifs-utils
sudo mkdir -p /mnt/truenas
sudo mount -t cifs //192.168.0.144/SMB_Share /mnt/truenas -o username=bessie,password=bessie,uid=1000,gid=1000
# Add to /etc/fstab for persistence
```

### 3. Set timezone
```bash
sudo timedatectl set-timezone America/New_York
```

### 4. Configure
```bash
cp .env.example .env
nano .env  # set BASE_URL=http://<your-vm-ip>:8000
```

### 5. Build & run
```bash
docker compose up -d --build
# First build ~5-10min (downloads Whisper medium model)
```

### 6. First poll
```bash
curl -X POST http://localhost:8000/admin/poll
```

### 7. Add to Pocket Casts
Pocket Casts → Add Podcast → Add URL → `http://<your-url>/feed.xml`

## Useful Commands
```bash
docker compose logs -f
curl -X POST http://localhost:8000/admin/poll
docker compose restart
docker compose up -d --build  # after code changes
```
