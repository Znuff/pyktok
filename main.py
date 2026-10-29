import asyncio
import json
import logging
import os
import random
import re
import shutil
import string
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

# --- Explore feed ----------------------------------------------------------
# Feature flag. The explore endpoint is reverse-engineered (TikTok ships no
# extractor for it) and can break without notice, so it ships disabled unless
# explicitly turned on with EXPLORE_ENABLED=1.
EXPLORE_ENABLED: bool = os.getenv("EXPLORE_ENABLED", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

# Region passed to TikTok's explore API. Empty = let TikTok infer it from the
# server's IP. Set e.g. EXPLORE_REGION=US to pin a region.
EXPLORE_REGION: str = os.getenv("EXPLORE_REGION", "") or ""

# Default category tab. 120 = "All" (see EXPLORE_CATEGORIES below).
EXPLORE_CATEGORY: int = int(os.getenv("EXPLORE_CATEGORY", "120") or "120")

# Items requested per explore page. The API treats this as a hint: it often
# returns fewer (and caps out around 30), so we ask for a healthy batch and
# top it up if needed.
EXPLORE_PAGE_SIZE: int = int(os.getenv("EXPLORE_PAGE_SIZE", "20") or "20")

# Max upstream requests per /api/explore call. The feed rotates a fresh batch
# each time, so re-asking is how we fill a page; this bounds the fan-out.
_EXPLORE_MAX_CALLS = 3

# Seconds to cache an explore response in memory (0 = off, the default).
# Explore rotates content on every call and the cursor is always "0", so any
# non-zero TTL makes "Load more" return the same cached batch until it lapses.
EXPLORE_CACHE_TTL: float = float(os.getenv("EXPLORE_CACHE_TTL", "0") or "0")

# Curated subset of TikTok's explore category enum (id -> label). The full enum
# lives in TikTok's explore-prefetch bundle; these are the stable headings.
EXPLORE_CATEGORIES: tuple[tuple[int, str], ...] = (
    (120, "All"),
    (104, "Comedy"),
    (119, "Singing & Dancing"),
    (112, "Sports"),
    (100, "Anime & Comics"),
    (111, "Food & Drink"),
    (117, "Fitness & Health"),
    (118, "Technology"),
    (113, "Animals"),
    (101, "Entertainment"),
    (106, "Family"),
    (110, "Lipsync"),
)

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


_AGE_RESTRICTED_MARKERS = (
    'comfortable for some audiences',
    'log in for access',
    'sign in to confirm your age',
    'age-restricted',
    'age restricted',
    'this video may be inappropriate for some users',
    'login required',
)

_GEO_BLOCKED_MARKERS = (
    'not available in your country',
    'not available in your region',
    'geo-restricted',
    'geo restricted',
    'video is not available in your location',
)

_UNAVAILABLE_MARKERS = (
    'video unavailable',
    'this video is unavailable',
    'video has been removed',
    'status code 404',
)


def _friendly_download_error(e: Exception) -> str:
    """Map a yt-dlp exception to an actionable, user-facing message."""
    msg = str(e).lower()
    if any(m in msg for m in _AGE_RESTRICTED_MARKERS):
        return "This video is age-restricted on TikTok."
    if any(m in msg for m in _GEO_BLOCKED_MARKERS):
        return "This video isn't available in the region this server is in."
    if any(m in msg for m in _UNAVAILABLE_MARKERS):
        return "This video is unavailable — it may have been deleted or made private."
    return "Something went wrong while downloading this video."


def _extract_meta_only(url: str) -> dict:
    """Blocking — extract metadata without downloading. Used in /resolve fallback."""
    with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


# ---------------------------------------------------------------------------
# SSRF protection: host allowlist for /resolve outbound HEAD
# ---------------------------------------------------------------------------

# Hosts we are willing to follow redirects to. Covers TikTok's public surface.
# The regional `tiktokcdn-*` entries are needed because explore cover images
# are served from e.g. p16-common-sign.tiktokcdn-eu.com.
_ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (
    "tiktok.com",
    "tiktokv.com",
    "tiktokcdn.com",
    "tiktokcdn-eu.com",
    "tiktokcdn-us.com",
    "tiktokcdn-va.com",
    "byteic.com",
    "ibyteimg.com",
    "musical.ly",
    "snssdk.com",
)


def _is_allowed_host(host: str) -> bool:
    host = host.lower().split(":", 1)[0]
    return any(host == sfx or host.endswith("." + sfx) for sfx in _ALLOWED_HOST_SUFFIXES)


# ---------------------------------------------------------------------------
# Explore feed
#
# TikTok's /explore page renders client-side from an unsigned JSON endpoint,
# /api/explore/item_list/. It requires a handful of "required fields" and a
# `ttwid` cookie that is only issued after loading the explore page once. We
# keep one long-lived AsyncClient, prime its cookie lazily, and refresh it if
# the API starts rejecting us. No request signing (X-Bogus/msToken) is needed.
# ---------------------------------------------------------------------------

_EXPLORE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_EXPLORE_WEBPAGE = "https://www.tiktok.com/explore"
_EXPLORE_API = "https://www.tiktok.com/api/explore/item_list/"

_explore_client: "httpx.AsyncClient | None" = None
_explore_lock = asyncio.Lock()
# (category, cursor) -> (cached_at, payload)
_explore_cache: dict[tuple[int, int], tuple[float, dict]] = {}


async def _explore_session(force: bool = False) -> httpx.AsyncClient:
    """Return the shared explore client, priming its `ttwid` cookie if needed.

    `force=True` clears cookies and re-fetches the explore page, which is what
    we do when the API stops returning items.
    """
    global _explore_client
    async with _explore_lock:
        if _explore_client is None:
            _explore_client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=20,
                headers={"User-Agent": _EXPLORE_UA},
            )
        if force or "ttwid" not in _explore_client.cookies:
            _explore_client.cookies.clear()
            try:
                await _explore_client.get(_EXPLORE_WEBPAGE)
            except Exception as e:
                log.warning("explore: cookie prime failed: %s", e)
        return _explore_client


def _explore_params(cursor: int, category: int) -> dict:
    """Query string TikTok's web explore prefetch sends. Order doesn't matter,
    but every field here is load-bearing: dropping categoryType/pullType gets
    you a 10201 "missing required fields"."""
    return {
        "language": "en",
        "aid": "1988",
        "app_language": "en",
        "app_name": "tiktok_web",
        "browser_language": "en-US",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": "5.0 (Windows)",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "device_platform": "web_pc",
        "focus_state": "true",
        "history_len": "2",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "os": "windows",
        "referer": "",
        "screen_height": "1080",
        "screen_width": "1920",
        "tz_name": "UTC",
        "webcast_language": "en",
        "verifyFp": f'verify_{"".join(random.choices(string.hexdigits, k=7))}',
        "categoryType": str(category),
        "pullType": "1",
        "count": str(EXPLORE_PAGE_SIZE),
        "enable_cache": "false",
        "isNonPersonalized": "false",
        "is_new_user": "false",
        "region": EXPLORE_REGION,
        "priority_region": EXPLORE_REGION,
        "cursor": str(cursor),
    }


def _normalize_explore_item(it: dict) -> dict | None:
    video_id = it.get("id")
    author = (it.get("author") or {}).get("uniqueId")
    if not video_id or not author:
        return None
    video = it.get("video") or {}
    stats = it.get("stats") or {}
    cover = video.get("cover") or video.get("originCover")
    desc = (it.get("desc") or "").strip()
    if len(desc) > 90:
        desc = desc[:87].rstrip() + "..."
    return {
        "id": video_id,
        "url": f"/@{author}/video/{video_id}",
        "author": author,
        "nickname": (it.get("author") or {}).get("nickname") or author,
        "desc": desc,
        "duration": video.get("duration") or 0,
        "thumb": f'/api/explore/thumb?url={urllib.parse.quote(cover, safe="")}' if cover else None,
        "diggs": stats.get("diggCount") or 0,
        "plays": stats.get("playCount") or 0,
    }


async def _fetch_explore(cursor: int, category: int) -> dict:
    """Fetch and normalize a full page of explore items.

    TikTok's explore feed rotates a fresh random batch on every request: the
    `cursor` it returns is always "0", `hasMore` is always true, and `count` is
    only a hint (batches of 3-20 are normal). So we ask repeatedly — each call
    is a new batch — and merge unique items until the page is full or we hit
    `_EXPLORE_MAX_CALLS`. Callers still de-duplicate by id across pages.
    """
    cache_key = (category, cursor)
    if EXPLORE_CACHE_TTL > 0:
        hit = _explore_cache.get(cache_key)
        if hit and time.time() - hit[0] < EXPLORE_CACHE_TTL:
            return hit[1]

    collected: dict[str, dict] = {}
    status = None
    refreshed = False
    got_response = False
    for attempt in range(1, _EXPLORE_MAX_CALLS + 1):
        # A non-zero status (100001 = stale/missing ttwid, 10201 = bad params)
        # gets one cookie refresh before we give up.
        force = status not in (0, None) and not refreshed
        if force:
            refreshed = True
        client = await _explore_session(force=force)
        try:
            resp = await client.get(
                _EXPLORE_API,
                params=_explore_params(cursor, category),
                headers={"Referer": _EXPLORE_WEBPAGE, "Accept": "application/json"},
            )
            data = resp.json()
            got_response = True
        except Exception as e:
            log.warning("explore: request failed (call %d): %s", attempt, e)
            data = {}
        status = data.get("statusCode")
        for it in data.get("itemList") or []:
            item = _normalize_explore_item(it)
            if item and item["id"] not in collected:
                collected[item["id"]] = item
        if len(collected) >= EXPLORE_PAGE_SIZE:
            break
        if status not in (0, None) and refreshed:
            log.info("explore: upstream statusCode=%s after refresh; giving up", status)
            break

    items = list(collected.values())[:EXPLORE_PAGE_SIZE]
    if not items and (not got_response or status not in (0, None)):
        raise HTTPException(status_code=502, detail="Explore feed is unavailable right now.")

    result = {
        "items": items,
        "cursor": 0,
        "hasMore": True,
    }
    if EXPLORE_CACHE_TTL > 0:
        if len(_explore_cache) > 128:
            _explore_cache.clear()
        _explore_cache[cache_key] = (time.time(), result)
    return result


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
            download_states[video_id] = {
                'status': 'error',
                'percent': 0,
                'error': _friendly_download_error(e),
            }
        # Evict terminal-state entry after grace window. Long enough for any
        # SSE client to see the final status, short enough to bound memory.
        loop.call_later(300, download_states.pop, video_id, None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _public_base_url(request: Request) -> str:
    """Absolute origin for og:/twitter: URLs, honoring a reverse proxy.

    Uvicorn isn't started with --proxy-headers here, so parse the forwarded
    headers ourselves. og media URLs must be absolute and HTTPS for the
    embeds to render.
    """
    proto = request.headers.get('x-forwarded-proto', request.url.scheme)
    proto = proto.split(',')[0].strip() or request.url.scheme
    host = request.headers.get('x-forwarded-host') or request.headers.get('host') or request.url.netloc
    host = host.split(',')[0].strip()
    return f'{proto}://{host}'


@app.get('/', response_class=HTMLResponse)
async def home(request: Request):
    try:
        recent_videos = _recent_videos(HOME_CAROUSEL_COUNT)
    except Exception as e:
        log.warning("home: failed to load recent videos: %s", e)
        recent_videos = []
    return templates.TemplateResponse(request, 'home.html', {
        'recent_videos': recent_videos,
        'explore_enabled': EXPLORE_ENABLED,
        'base_url': _public_base_url(request),
    })


@app.get('/explore', response_class=HTMLResponse)
async def explore_page(request: Request):
    if not EXPLORE_ENABLED:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, 'explore.html', {
        'categories': EXPLORE_CATEGORIES,
        'default_category': EXPLORE_CATEGORY,
    })


@app.get('/api/explore')
async def explore_api(category: int | None = None, cursor: int = 0):
    if not EXPLORE_ENABLED:
        raise HTTPException(status_code=404)
    if category is None:
        category = EXPLORE_CATEGORY
    try:
        return await _fetch_explore(cursor, category)
    except HTTPException:
        raise
    except Exception as e:
        log.warning("explore: fetch failed: %s", e)
        raise HTTPException(status_code=502, detail='Explore feed is unavailable right now.')


@app.get('/api/explore/thumb')
async def explore_thumb(url: str):
    """Host-allowlisted image proxy. Explore covers are signed TikTok CDN URLs
    that expire and may be referrer-checked, so we stream them ourselves."""
    if not EXPLORE_ENABLED:
        raise HTTPException(status_code=404)
    try:
        parsed = httpx.URL(url)
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid image URL.')
    if parsed.scheme not in ('http', 'https') or not parsed.host or not _is_allowed_host(parsed.host):
        raise HTTPException(status_code=400, detail='Image host not allowed.')

    try:
        client = await _explore_session()
        resp = await client.send(
            client.build_request('GET', url, headers={'Referer': 'https://www.tiktok.com/'}),
            stream=True,
        )
    except Exception as e:
        log.warning("explore: thumb fetch failed: %s", e)
        raise HTTPException(status_code=502, detail='Image fetch failed.')

    # The shared client follows redirects; re-check the final host.
    if resp.status_code != 200 or (resp.url.host and not _is_allowed_host(resp.url.host)):
        await resp.aclose()
        raise HTTPException(status_code=502, detail='Image fetch failed.')
    ctype = resp.headers.get('content-type', '')
    if not ctype.startswith('image/'):
        await resp.aclose()
        raise HTTPException(status_code=502, detail='Not an image.')

    async def _stream():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(_stream(), media_type=ctype, headers={
        'Cache-Control': 'public, max-age=3600',
        'X-Accel-Buffering': 'no',
    })


async def _resolve_tiktok_url(url: str) -> RedirectResponse:
    """Resolve an allowed TikTok URL to its local canonical video page."""
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

    if re.search(r'/@([^/]+)/live', canonical):
        raise HTTPException(
            status_code=400,
            detail="pyktok doesn't support TikTok Live streams yet.",
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
    return await _resolve_tiktok_url(values[0])


_SHORT_LINK_HOSTS = ('vm.tiktok.com', 'vt.tiktok.com')


@app.get('/{short_code}')
async def resolve_short_url(short_code: str):
    """Resolve TikTok's /Z.../ short-link paths on this host."""
    if not re.fullmatch(r'Z[A-Za-z0-9]+', short_code):
        raise HTTPException(status_code=404)
    for host in _SHORT_LINK_HOSTS:
        try:
            return await _resolve_tiktok_url(f'https://{host}/{short_code}/')
        except HTTPException:
            if host == _SHORT_LINK_HOSTS[-1]:
                raise


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
    base_url = _public_base_url(request)
    canonical_url = f'{base_url}{request.url.path}'

    # Reject photo posts up front — yt-dlp will fail anyway, and we want a
    # clear error message instead of a generic download failure.
    if re.match(r'^@[^/]+/photo/\d+$', path):
        return templates.TemplateResponse(request, 'player.html', {
            'ready': False,
            'video_id': '0',
            'username': path.split('/')[0].lstrip('@'),
            'tiktok_url': f'https://www.tiktok.com/{path}',
            'error_message': "pyktok doesn't support TikTok photo posts — only videos.",
            'base_url': base_url,
            'canonical_url': canonical_url,
        })

    # Live streams aren't downloadable — show a clear message rather than a 404.
    live_match = re.match(r'^@([^/]+)/live$', path)
    if live_match:
        return templates.TemplateResponse(request, 'player.html', {
            'ready': False,
            'video_id': '0',
            'username': live_match.group(1),
            'tiktok_url': f'https://www.tiktok.com/{path}',
            'error_message': "pyktok doesn't support TikTok Live streams yet.",
            'base_url': base_url,
            'canonical_url': canonical_url,
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
            'base_url': base_url,
            'canonical_url': canonical_url,
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
        'base_url': base_url,
        'canonical_url': canonical_url,
    })
