# pyktok

A small viewing proxy for TikTok. Paste a `tiktok.com` URL, get back the
video, a thumbnail, and the metadata, served from a local cache.

It runs a FastAPI app that hands the URL to `yt-dlp`, stores the result
under `data/<video_id>/`, and serves it back through a player page.
The metadata yt-dlp returns (formats, cookies, headers) is saved
verbatim into `meta.json` alongside the mp4 and jpg, so subsequent
requests skip the upstream call entirely.

## Run it

The image is published to GitHub Container Registry on every push to
`main` and on every `v*` tag.

```sh
docker run -d \
  --name pyktok \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v ./data:/app/data \
  ghcr.io/znuff/pyktok:latest
```

Or with compose:

```yaml
services:
  pyktok:
    image: ghcr.io/znuff/pyktok:latest
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

Then open <http://localhost:8000>.

The volume holds the cache. Drop it and you start clean.

### Behind nginx

`nginx_sample.conf` is included. It terminates TLS, disables proxy
buffering on the `/api/progress/...` SSE stream, and forwards Range
requests to the app. Copy it, edit the server name and the TLS paths,
`ln -s` into `sites-enabled`, reload.

## Environment

| Var                        | Default | What it does                                                                 |
|----------------------------|---------|------------------------------------------------------------------------------|
| `VIDEO_RETENTION_DAYS`     | `0`     | Delete videos older than N days on cleanup. `0` keeps everything.            |
| `CLEANUP_DISK_THRESHOLD_GB`| `0`     | When free disk falls below N GB, run age + LRU cleanup. `0` disables it.     |
| `HOME_CAROUSEL_COUNT`      | `10`    | Recent-videos carousel on the homepage. `0` hides it.                       |
| `MAX_CONCURRENT_DOWNLOADS` | `2`     | yt-dlp runs in a thread pool; this caps how many go at once.                 |

## Why yt-dlp

Hand-rolling a TikTok extractor is a losing game. TikTok rotates
signing keys, changes URL shapes, and ships anti-bot challenges on a
timescale measured in days. Every time a new client tries to scrape
them directly, it breaks within a week.

`yt-dlp` already does this. It ships a TikTok extractor maintained by
people whose full-time job is keeping up with those changes, and it
auto-updates through PyPI. We hand it a URL, it gives us back a direct
video URL, the metadata, and a thumbnail. pyktok is just a thin FastAPI
shell around that: a paste box, a cache, and a player page.

The tradeoff is dependency on a third-party project that could slow
down or stop, and a startup that requires network access to fetch
yt-dlp updates. Both are acceptable for a self-hosted viewing tool.

## How it works

1. `GET /` renders a paste box and, if the carousel is on, the most
   recent downloads.
2. `POST /resolve` takes any tiktok URL, follows the shortlink, and
   redirects to `/@user/video/<id>`.
3. That route checks `data/<id>/meta.json`. If it exists, render the
   player immediately. If not, kick off `_sync_download` in the thread
   pool and render a loading page that polls `/api/progress/<id>` over
   SSE.
4. The download writes `video.mp4`, `thumbnail.jpg`, `meta.json`, and a
   `downloaded_at` float. The player page reads width/height from
   `meta.json` so the poster doesn't render as a 300x150 default.

`data/` is a flat directory of one folder per video ID. Cleanup walks
it, sorts by `downloaded_at`, and removes the oldest first.

## Development

Local venv, then install the deps and run uvicorn:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --reload
```

`ffmpeg` must be on the host PATH — yt-dlp needs it for the thumbnail
postprocessor. The container installs it for you; on a workstation
install it through your package manager.

`requirements.txt` pins nothing. `uvicorn[standard]` pulls in the
`watchfiles` and `httptools` extras so `--reload` actually works.

The entrypoint of the Docker image runs `pip install --upgrade yt-dlp`
on every start, which keeps the extractor working when TikTok changes
their site. Do the same in your venv when yt-dlp starts failing.

## Notes

Currently it only handles VIDEOS. Photos, slideshows, and other
TikTok post types are not supported.

Feeds are not supported and not planned. That means the homepage
(`/foryou`, `/following`), user profile pages, hashtag pages, and any
other listing endpoint. The app only resolves a single video URL to a
single video. If you want to browse TikTok, use the website or the
app; pyktok is for watching a specific link someone sent you without
opening either.
