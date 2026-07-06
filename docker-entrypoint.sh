#!/bin/sh
set -e

# ---------------------------------------------------------------------------
# Clean up any partial pip install dirs from a prior interrupted upgrade.
# `~*dlp*` is pip's naming for half-installed packages.
# ---------------------------------------------------------------------------
find /app/.venv -maxdepth 6 -type d -name '~*dlp*' -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# yt-dlp upgrade on startup. Runs as pyktok (the image's USER); the venv is
# user-owned, so pip can replace dist-info directories without permission
# errors.
# ---------------------------------------------------------------------------
echo "[pyktok] Upgrading yt-dlp to latest..."
pip install -q --upgrade yt-dlp
echo "[pyktok] $(pip show yt-dlp | grep Version)"

# ---------------------------------------------------------------------------
# Background upgrader: every 12 hours, check for a new yt-dlp release.
# Only restarts if the version actually changed.
#
# Restart mechanism:
#   - `exec uvicorn` below replaces this shell as PID 1
#   - kill -TERM 1 sends SIGTERM to uvicorn
#   - uvicorn shuts down gracefully, container exits, Docker restarts it
#   - on restart the entrypoint runs again with the new yt-dlp already installed
# ---------------------------------------------------------------------------
(
  while true; do
    sleep 43200  # 12 hours
    OLD_VER=$(pip show yt-dlp | grep ^Version | cut -d' ' -f2)
    pip install -q --upgrade yt-dlp
    NEW_VER=$(pip show yt-dlp | grep ^Version | cut -d' ' -f2)
    if [ "$OLD_VER" != "$NEW_VER" ]; then
      echo "[pyktok] yt-dlp upgraded $OLD_VER → $NEW_VER — restarting..."
      kill -TERM 1
    else
      echo "[pyktok] yt-dlp $OLD_VER is already the latest"
    fi
  done
) &

echo "[pyktok] Starting uvicorn..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
