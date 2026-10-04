# AGENTS.md — pyktok build instructions

TikTok viewing proxy. FastAPI+Python. Reads `tiktok.com` URLs, proxies through
`yt-dlp`, serves cached video + metadata.

**Status: built, working, deployed.** Read existing files before changing anything.
Do not recreate from scratch.

## File tree

```
<project-root>/
├── main.py                — FastAPI app, all routes, download state, cleanup
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh   — yt-dlp auto-upgrade + uvicorn launch
├── AGENTS.md              — this file
├── templates/
│   ├── home.html          — input + recent-videos carousel
│   ├── explore.html       — experimental explore feed (flag-gated)
│   └── player.html        — loading + player states
├── static/
│   ├── style.css
│   ├── player.js
│   ├── explore.js
│   ├── favicon.svg
│   └── logo.svg
└── data/{tiktok_id}/
    ├── meta.json
    ├── video.mp4
    ├── thumbnail.jpg
    └── downloaded_at      — float timestamp, cleanup ordering
```

## Environment

| Var | Default | Effect |
|---|---|---|
| `VIDEO_RETENTION_DAYS` | `0` off | age-based cleanup |
| `CLEANUP_DISK_THRESHOLD_GB` | `0` off | LRU eviction on low disk |
| `HOME_CAROUSEL_COUNT` | `10` | recent-videos carousel, `0` disables |
| `EXPLORE_ENABLED` | `0` off | experimental `/explore` feed |
| `EXPLORE_REGION` | `""` | region sent to explore API; empty = from server IP |
| `EXPLORE_CATEGORY` | `120` | default category tab (`120` = All) |
| `EXPLORE_PAGE_SIZE` | `8` | items per explore batch |
| `EXPLORE_CACHE_TTL` | `60` | seconds to cache an explore batch, `0` disables |

## Python

Container has `.venv/`. **Always `.venv/bin/python` / `.venv/bin/pip`**. Never
`pip install` system-wide. You must NOT start the server — container runs it.

## ⚠ DO NOT touch the running uvicorn

The app runs inside a Docker container managed by its own orchestrator. The
container owns the uvicorn process — killing it, restarting it, or starting a
second one for "testing" breaks the live service the user is using. Do not try
to verify your changes by restarting the server.

**Forbidden**:
- `pkill -f uvicorn`
- `kill <uvicorn pid>`
- Starting a second uvicorn on a different port
- `setsid .venv/bin/uvicorn ... &` to "smoke test" changes

**How to verify a change**:
- Edit the file. The container watches the source and reloads on its own, OR
  the user rebuilds/restarts the container. Either way: not your job.
- For static syntax sanity only, you may run
  `python -c "import ast; ast.parse(open('main.py').read())"` or a one-shot
  `.venv/bin/python -c '...'` snippet that does NOT bind a port.
- `curl http://localhost:8000/...` reads the current running response, but it
  reflects the **old** code until the container reloads.

**If something seems broken on the running server**: tell the user, don't fix it by restarting.

## Critical gotchas

### Starlette 1.0.0 — `TemplateResponse` signature
Old: `templates.TemplateResponse('home.html', {'request': request, ...})` — broken.
New: `templates.TemplateResponse(request, 'home.html', {...})`. `request` first arg, not in context.

### `_sync_download` signature
`download_states` is module-level global. Do NOT pass as arg. `run_in_executor` takes
positional args — closures/lambdas broken. Function reads `download_states` directly.

### `<video>` needs explicit `width`/`height`
Else browser defaults 300×150 before metadata. Poster appears tiny. Pass from `meta.json`:
```html
<video width="{{ width }}" height="{{ height }}" ...>
```

### SSE buffering (two layers)
1. FastAPI: `headers={'X-Accel-Buffering': 'no'}` on StreamingResponse
2. nginx: `proxy_buffering off;` in `nginx_sample.conf`

### No `StaticFiles` mount for `/data`
`StaticFiles` ignores Range. Use explicit `FileResponse` routes:
- `GET /data/{video_id}/video.mp4`
- `GET /data/{video_id}/thumbnail.jpg`

### Explore feed is flag-gated + reverse-engineered
`/explore` and `/api/explore*` are 404 no-ops unless `EXPLORE_ENABLED=1`. The
feed comes from TikTok's private, unsigned web API `GET /api/explore/item_list/`
(see `_fetch_explore` in main.py). Three things to know:
- It needs a `ttwid` cookie from loading `https://www.tiktok.com/explore`; the
  module-level `_explore_client` primes/refreshes it via `_explore_session`.
- It does **not** paginate: the returned `cursor` is always `"0"`, `hasMore` is
  always true, and every call is a fresh rotating batch. `explore.js` de-dupes
  by video id, and the UI is "Load more", not infinite scroll.
- It needs `categoryType` + `pullType` plus the browser params or it returns
  `statusCode 10201` ("missing required fields"); a missing/expired `ttwid`
  gives `100001`. Both trigger one cookie refresh + retry.

### Regional TikTok CDN hosts
Explore covers live on `*.tiktokcdn-eu.com` (also `-us`/`-va`). If TikTok adds
a new region, add its `tiktokcdn-*.com` suffix to `_ALLOWED_HOST_SUFFIXES` or
`/api/explore/thumb` returns 400 and the grid shows broken images.

### Route order: `/explore` before `/{short_code}`
FastAPI matches in registration order. The `/{short_code}` short-link catcher
would swallow `/explore` if registered first. Keep `/explore` and the
`/api/explore*` routes above it.

### yt-dlp auto-upgrade
Dockerfile builds and installs the venv as the unprivileged user, so the venv is
user-owned from the start. `docker-entrypoint.sh` runs as that same user, so
`pip install --upgrade yt-dlp` works without permission errors.

Stale partial-install dirs from a prior interrupted upgrade (`~*dlp*`) can still
cause `WARNING: Ignoring invalid distribution ~*dlp` warnings. The entrypoint
cleans them on every start:
```sh
find /app/.venv -maxdepth 6 -type d -name '~*dlp*' -exec rm -rf {} + 2>/dev/null || true
```

## Implemented (main.py)

- `GET /` — `home.html`, passes `recent_videos` (newest-first, by `downloaded_at`) + `explore_enabled`
- `POST /resolve` — httpx HEAD redirect-follow → 303 to `/@user/video/id`; yt-dlp fallback
- `GET /explore` — `explore.html`; 404 unless `EXPLORE_ENABLED`; passes `categories` + `default_category`
- `GET /api/explore?category=&cursor=` — normalized JSON batch; 404 unless enabled; `_fetch_explore` refreshes cookie once, retries
- `GET /api/explore/thumb?url=` — host-allowlisted streaming image proxy for signed CDN covers
- `GET /{path:path}` — catches `@user/video/id`, checks `data/{id}/meta.json`. If ready → player. Else kick `_sync_download` in `loop.run_in_executor`, render loading state.
- `GET /api/progress/{video_id}` — SSE, 0.3s poll, 600 iters (~3min)
- `GET /data/{video_id}/video.mp4` — `FileResponse` (Range)
- `GET /data/{video_id}/thumbnail.jpg` — `FileResponse`
- `download_states: dict[str, dict]` — module global, in-memory, keyed by ID
- `_video_dirs_by_age()` / `_recent_videos(n)` — newest-first, skip missing meta/thumb, trunc desc to 60 chars, never raises
- `_explore_session()` / `_explore_params()` / `_fetch_explore()` — explore cookie priming/refresh + rotating-batch fetch; `EXPLORE_CATEGORIES` enum in config block

## Implemented (templates/static)

**home.html** — black bg, custom display font, input + Go. Direct-match `tiktok.com/@user/video/id` → navigate; else POST `/resolve`. Carousel: `flex-direction:column` body, mobile horizontal scroll with `scroll-snap-type:x mandatory`, ≥640px `grid auto-fill,minmax(140px,1fr)`. 140px cards, 9:16 thumb, `@author` + 1-line desc, click → `/@user/video/id`.

**player.html** — loading: SSE progress bar, "Processing your video, please be patient", error state. Player: `<video>` w/ width+height from meta, no autoplay/muted. Play btn overlay (`#play-btn`) shown if autoplay blocked.
```
.overlay
  .overlay-top
    .author-section
      #author-handle   pill, click toggles desc
      #desc-badge      pill, appears below author
    .options-wrapper
      #options-btn     circular pill, share SVG
      #options-panel   popup dropdown: Download, Share, Open on TikTok
```

**style.css** — progress: `position:fixed;top:0;height:3px;background:#fe2c55`. `.video-wrapper` `position:relative;max-height:100dvh`. `video` `max-height:100dvh;max-width:100vw;object-fit:contain`. Pills: `rgba(0,0,0,0.38)` + `backdrop-filter:blur(6px)`. `.desc-badge` `position:absolute;top:calc(100%+6px);opacity:0` → `1` on `.visible`. `.options-panel` `position:absolute;top:calc(100%+8px);right:0`, fades+scales in on `.open`. `.play-btn` centered 72px circle. `.home-carousel-card` 140px wide, `flex:0 0 140px`, `scroll-snap-align:start`, `border-radius:10px`, hover `translateY(-2px)`.

**player.js** — `loadVolume`/`saveVolume` localStorage keys `pyktok_volume`, `pyktok_muted`. Volume restored pre-play; `volumechange` saves both. `video.play()` → if rejected show `#play-btn`, no muted fallback. Controls: `mouseenter` add attr, `mouseleave` remove (unless options panel open). Touch: show 3s then hide. Options: toggle `.open`, close on outside click. Desc: toggle `.visible`, close on outside. Share: `navigator.clipboard.writeText(location.href)`, "Copied!" 1.5s. Play btn: click → `play()`, hide on success or `play` event.

**explore.html / explore.js** — sticky header (back arrow + "Explore"), sticky horizontally-scrolling category pill bar (`data-category`, `.active`), responsive card grid (`auto-fill,minmax(150px,1fr)`), "Load more" button, error `<p>`. `explore.js` holds `{category, seen:Set, busy}`; fetches `/api/explore?category=`, de-dupes by `id`, appends `.explore-card` anchors (`href` → `/@user/video/id`), resets on category click. `window.EXPLORE_CONFIG.defaultCategory` set inline. `style.css` gains `.explore-*` (mirrors home-card styling) and `.home-explore-link`.

## Smoke tests

```bash
curl -s http://localhost:8000/ | head -5
curl -s 'http://localhost:8000/@x/video/123' | grep -i 'processing'
curl -s 'http://localhost:8000/api/progress/123' --max-time 2
# explore (only meaningful when EXPLORE_ENABLED=1)
curl -s http://localhost:8000/explore | head -5
curl -s 'http://localhost:8000/api/explore?category=120' | head -c 300
```
Reads running code. Reload after edits.

## Open issues / scratchpad

<!-- Append new issues here. Do not delete resolved entries silently; strikethrough them. -->
