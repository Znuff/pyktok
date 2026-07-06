import asyncio
import json
import logging
import os
import re
import shutil
import time
import urllib.parse
from pathlib import Path

import httpx
import yt_dlp
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pyktok")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------

# Delete videos older than this many days (0 or unset = keep forever)
VIDEO_RETENTION_DAYS: float = float(os.getenv("VIDEO_RETENTION_DAYS", "0") or "0")

# Trigger age-based + LRU cleanup when free disk space falls below this threshold in GB
# (0 or unset = no disk-pressure cleanup)
CLEANUP_DISK_THRESHOLD_GB: float = float(os.getenv("CLEANUP_DISK_THRESHOLD_GB", "0") or "0")

# Number of recent videos to show in a mini-carousel on the homepage
# (0 or unset = disabled). Newest first, by downloaded_at timestamp.
HOME_CAROUSEL_COUNT: int = int(os.getenv("HOME_CAROUSEL_COUNT", "10") or "0")

# Max size of form fields in bytes (url= is the only one we accept).
# Default 8 KiB — a real tiktok URL is well under 1 KiB.
MAX_FORM_BYTES: int = int(os.getenv("MAX_FORM_BYTES", str(8 * 1024)) or str(8 * 1024))

# DoS guardrails
MAX_CONCURRENT_DOWNLOADS: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2") or "2")
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
MAX_DOWNLOAD_STATES: int = int(os.getenv("MAX_DOWNLOAD_STATES", "200") or "200")

DATA_DIR = Path("data")

# In-memory download state keyed by TikTok video ID
# Shape: {'status': 'processing'|'ready'|'error', 'percent': int, 'description': str, 'author': str}
download_states: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def _video_dirs_by_age() -> list[tuple[float, Path]]:
    """Return list of (download_time, dir) for all video dirs, oldest first.

    Reads only the tiny `downloaded_at` file (a plain float timestamp written
    at download time). Falls back to meta.json mtime for videos that predate
    this file. Never parses meta.json.
    """
    entries = []
    if not DATA_DIR.exists():
        return entries
    for d in DATA_DIR.iterdir():
        if not d.is_dir():
            continue
        ts_file = d / "downloaded_at"
        if ts_file.exists():
            try:
                ts = float(ts_file.read_text().strip())
            except (ValueError, OSError):
                ts = ts_file.stat().st_mtime
        else:
            # Fallback for videos downloaded before this file was introduced
            meta = d / "meta.json"
            ts = meta.stat().st_mtime if meta.exists() else d.stat().st_mtime
        entries.append((ts, d))
    entries.sort(key=lambda x: x[0])  # oldest first
    return entries


def _free_gb() -> float:
    usage = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else ".")
    return usage.free / (1024 ** 3)


def _recent_videos(n: int) -> list[dict]:
    """Return up to n most recent videos as dicts for the homepage carousel.

    Newest first (by downloaded_at, falling back to meta.json mtime).
    Skips entries with missing meta.json or thumbnail.jpg so broken
    cache entries never produce 404s in the carousel.
    """
    if n <= 0 or not DATA_DIR.exists():
        return []
    out: list[dict] = []
    for _, d in reversed(_video_dirs_by_age()):
        meta_path = d / "meta.json"
        thumb_path = d / "thumbnail.jpg"
        if not meta_path.exists() or not thumb_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        video_id = meta.get("id") or d.name
        uploader = meta.get("uploader") or ""
        desc = (meta.get("description") or "").strip()
        if len(desc) > 60:
            desc = desc[:57].rstrip() + "..."
        out.append({
            "video_id": video_id,
            "uploader": uploader,
            "description": desc,
            "thumbnail_url": f"/data/{video_id}/thumbnail.jpg",
        })
        if len(out) >= n:
            break
    return out


def _delete_video_dir(d: Path) -> None:
    vid_id = d.name
    try:
        shutil.rmtree(d)
        download_states.pop(vid_id, None)
        log.info("cleanup: removed %s", d)
    except Exception as e:
        log.warning("cleanup: failed to remove %s: %s", d, e)


def run_cleanup() -> None:
    """
    1. Delete videos older than VIDEO_RETENTION_DAYS (if set).
    2. If free disk < CLEANUP_DISK_THRESHOLD_GB, evict oldest videos until
       free space is back above the threshold.
    Active downloads are never deleted.
    """
    now = time.time()
    dirs = _video_dirs_by_age()

    # --- Age-based retention ---
    if VIDEO_RETENTION_DAYS > 0:
        cutoff = now - VIDEO_RETENTION_DAYS * 86400
        for mtime, d in dirs:
            if mtime < cutoff and d.name not in download_states:
                _delete_video_dir(d)

    # --- Disk-pressure eviction ---
    if CLEANUP_DISK_THRESHOLD_GB > 0 and _free_gb() < CLEANUP_DISK_THRESHOLD_GB:
        log.info("cleanup: disk pressure triggered (free=%.2f GB < %.2f GB threshold)",
                 _free_gb(), CLEANUP_DISK_THRESHOLD_GB)
        # Re-read dirs after age-based pass
        dirs = _video_dirs_by_age()
        for _, d in dirs:
            if _free_gb() >= CLEANUP_DISK_THRESHOLD_GB:
                break
            if d.name not in download_states:
                _delete_video_dir(d)


async def _cleanup_loop() -> None:
    """Background task: run cleanup every hour. Runs blocking I/O in the
    default executor so the event loop is never stalled by dir scans or
    recursive deletes."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            await loop.run_in_executor(None, run_cleanup)
        except Exception as e:
            log.error("cleanup loop error: %s", e)
        await asyncio.sleep(3600)


@app.on_event("startup")
async def startup_event() -> None:
    if VIDEO_RETENTION_DAYS > 0 or CLEANUP_DISK_THRESHOLD_GB > 0:
        log.info(
            "cleanup enabled — retention=%.1f days, disk_threshold=%.2f GB",
            VIDEO_RETENTION_DAYS,
            CLEANUP_DISK_THRESHOLD_GB,
        )
        asyncio.create_task(_cleanup_loop())
    else:
        log.info("cleanup disabled (VIDEO_RETENTION_DAYS and CLEANUP_DISK_THRESHOLD_GB not set)")


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def _progress_hook(d: dict, video_id: str):
    if d['status'] == 'downloading':
        downloaded = d.get('downloaded_bytes') or 0
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        percent = int(downloaded / total * 100) if total else 0
        if video_id in download_states:
            download_states[video_id]['percent'] = percent
    elif d['status'] == 'finished':
        if video_id in download_states:
            download_states[video_id]['percent'] = 99  # FFmpeg still running
    elif d['status'] == 'error':
        download_states[video_id] = {'status': 'error', 'percent': 0}


def _sync_download(video_id: str, url: str) -> dict:
    """Blocking — run in executor. Downloads video+thumbnail, writes meta.json."""
    log.info("download start  [%s]  %s", video_id, url)
    t0 = time.time()
    hook = lambda d: _progress_hook(d, video_id)
    opts = {
        'outtmpl': {
            'default': f'data/{video_id}/video.%(ext)s',
            'thumbnail': f'data/{video_id}/thumbnail',
        },
        'writethumbnail': True,
        'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}],
        'no_part': True,
        'noprogress': True,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [hook],
    }
    Path(f'data/{video_id}').mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        clean = ydl.sanitize_info(info)
    elapsed = time.time() - t0
    log.info("download done   [%s]  %.1fs  (%s @ %s)",
             video_id, elapsed,
             info.get('title', '(no title)'), info.get('uploader', ''))
    with open(f'data/{video_id}/meta.json', 'w') as f:
        json.dump(clean, f)
    # Separate tiny file — cleanup scanner reads only this, never parses meta.json
    Path(f'data/{video_id}/downloaded_at').write_text(str(time.time()))
    return clean


def _extract_meta_only(url: str) -> dict:
    """Blocking — extract metadata without downloading. Used in /resolve fallback."""
    with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


# ---------------------------------------------------------------------------
# SSRF protection: host allowlist for /resolve outbound HEAD
# ---------------------------------------------------------------------------

# Hosts we are willing to follow redirects to. Covers TikTok's public surface.
_ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (
    "tiktok.com",
    "tiktokv.com",
    "tiktokcdn.com",
    "byteic.com",
    "ibyteimg.com",
    "musical.ly",
    "snssdk.com",
)


def _is_allowed_host(host: str) -> bool:
    host = host.lower().split(":", 1)[0]
    return any(host == sfx or host.endswith("." + sfx) for sfx in _ALLOWED_HOST_SUFFIXES)


async def download_video_async(video_id: str, url: str):
    loop = asyncio.get_event_loop()
    async with DOWNLOAD_SEMAPHORE:
        try:
            info = await loop.run_in_executor(None, _sync_download, video_id, url)
            download_states[video_id] = {
                'status': 'ready',
                'percent': 100,
                'description': info.get('description', ''),
                'author': info.get('uploader', ''),
            }
        except Exception as e:
            log.error("download error  [%s]  %s", video_id, e)
            download_states[video_id] = {'status': 'error', 'percent': 0, 'error': str(e)}
        # Evict terminal-state entry after grace window. Long enough for any
        # SSE client to see the final status, short enough to bound memory.
        loop.call_later(300, download_states.pop, video_id, None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get('/', response_class=HTMLResponse)
async def home(request: Request):
    try:
        recent_videos = _recent_videos(HOME_CAROUSEL_COUNT)
    except Exception as e:
        log.warning("home: failed to load recent videos: %s", e)
        recent_videos = []
    return templates.TemplateResponse(request, 'home.html', {'recent_videos': recent_videos})


@app.post('/resolve')
async def resolve_url(request: Request):
    # Read raw body once, then re-parse form from it. Reading request.body()
    # after FastAPI's Form(...) dependency has already touched the stream
    # raises RuntimeError("Stream consumed"), so we do body+parse ourselves
    # and skip the Form() dependency entirely.
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(status_code=413, detail='Request body too large.')
    try:
        form = urllib.parse.parse_qs(body.decode('utf-8'), strict_parsing=True)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail='Invalid form body.')
    values = form.get('url')
    if not values or not values[0].strip():
        raise HTTPException(status_code=400, detail='Please paste a TikTok URL.')
    url = values[0]
    # Validate scheme + allowlist host BEFORE any outbound request.
    try:
        parsed = httpx.URL(url)
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid URL.')
    if parsed.scheme not in ('http', 'https'):
        raise HTTPException(status_code=400, detail='URL must be http(s).')
    if not parsed.host or not _is_allowed_host(parsed.host):
        raise HTTPException(status_code=400, detail='URL host not allowed.')

    canonical = url
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            resp = await client.head(url, headers={'User-Agent': 'Mozilla/5.0'})
            # Re-check final host after redirects — httpx followed them
            # but it didn't know about our allowlist.
            if resp.url.host and not _is_allowed_host(resp.url.host):
                raise HTTPException(status_code=400, detail='Redirect target not allowed.')
            canonical = str(resp.url)
    except HTTPException:
        raise
    except Exception:
        pass

    photo_match = re.search(r'/@([^/]+)/photo/(\d+)', canonical)
    if photo_match:
        raise HTTPException(
            status_code=400,
            detail="pyktok doesn't support TikTok photo posts — only videos.",
        )

    match = re.search(r'/@([^/]+)/video/(\d+)', canonical)
    if match:
        user, vid_id = match.groups()
        return RedirectResponse(f'/@{user}/video/{vid_id}', status_code=303)

    # Fallback: yt-dlp resolve (yt-dlp will do its own fetching; same allowlist
    # applies at download time via tiktok_url construction in catch_all).
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, _extract_meta_only, url)
        # Reject photo posts (TikTok slideshows). yt-dlp returns them as a
        # playlist of image entries — no playable video, would 500/garbage
        # downstream.
        if info.get('_type') == 'playlist' or info.get('entries') or info.get('vcodec') == 'none':
            raise HTTPException(
                status_code=400,
                detail="pyktok doesn't support TikTok photo posts — only videos.",
            )
        uploader = info.get('uploader') or info.get('creator') or info.get('channel')
        vid_id = info.get('id')
        if not uploader or not vid_id:
            raise HTTPException(status_code=400, detail='No video found at that URL.')
        return RedirectResponse(f'/@{uploader}/video/{vid_id}', status_code=303)
    except HTTPException:
        raise
    except Exception as e:
        log.warning("resolve failed for %s: %s", url, e)
        raise HTTPException(status_code=400, detail='Could not resolve that TikTok URL. Make sure it points to a video (photo posts and other non-video links are not supported).')


@app.get('/api/progress/{video_id}')
async def progress_stream(video_id: str):
    async def generate():
        for _ in range(600):  # max ~3 min
            state = download_states.get(video_id, {'status': 'not_found', 'percent': 0})
            yield f'data: {json.dumps(state)}\n\n'
            if state['status'] in ('ready', 'error', 'not_found'):
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(
        generate(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.api_route('/data/{video_id}/video.mp4', methods=['GET', 'HEAD'])
async def serve_video(video_id: str):
    path = Path(f'data/{video_id}/video.mp4')
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type='video/mp4')


@app.api_route('/data/{video_id}/thumbnail.jpg', methods=['GET', 'HEAD'])
async def serve_thumbnail(video_id: str):
    path = Path(f'data/{video_id}/thumbnail.jpg')
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type='image/jpeg')


@app.get('/{path:path}')
async def catch_all(path: str, request: Request):
    # Reject photo posts up front — yt-dlp will fail anyway, and we want a
    # clear error message instead of a generic download failure.
    if re.match(r'^@[^/]+/photo/\d+$', path):
        return templates.TemplateResponse(request, 'player.html', {
            'ready': False,
            'video_id': '0',
            'username': path.split('/')[0].lstrip('@'),
            'tiktok_url': f'https://www.tiktok.com/{path}',
            'error_message': "pyktok doesn't support TikTok photo posts — only videos.",
        })

    match = re.match(r'^@([^/]+)/video/(\d+)$', path)
    if not match:
        raise HTTPException(status_code=404)

    username, video_id = match.groups()
    tiktok_url = f'https://www.tiktok.com/@{username}/video/{video_id}'

    meta_path = Path(f'data/{video_id}/meta.json')
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        return templates.TemplateResponse(request, 'player.html', {
            'video_id': video_id,
            'username': username,
            'tiktok_url': tiktok_url,
            'ready': True,
            'description': meta.get('description', ''),
            'author': meta.get('uploader', username),
            'thumbnail_url': f'/data/{video_id}/thumbnail.jpg',
            'video_url': f'/data/{video_id}/video.mp4',
            'duration': meta.get('duration', 0),
            'width': meta.get('width', 0),
            'height': meta.get('height', 0),
        })

    # Not cached — kick off download and show loading page.
    # Cap in-memory state to bound memory under floods.
    if video_id not in download_states:
        if len(download_states) >= MAX_DOWNLOAD_STATES:
            raise HTTPException(
                status_code=503,
                detail='Server busy; too many concurrent downloads. Try again shortly.',
            )
        download_states[video_id] = {'status': 'processing', 'percent': 0}
        asyncio.create_task(download_video_async(video_id, tiktok_url))

    return templates.TemplateResponse(request, 'player.html', {
        'video_id': video_id,
        'username': username,
        'tiktok_url': tiktok_url,
        'ready': False,
    })
