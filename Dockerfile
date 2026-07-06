FROM python:3.12-slim

# ffmpeg: required by yt-dlp's FFmpegThumbnailsConvertor postprocessor
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Non-root user; UID 1000 matches the default first user on most Linux hosts.
RUN useradd -u 1000 -r -s /bin/sh -d /app pyktok

WORKDIR /app

# WORKDIR created /app as root; give it to pyktok so the USER switch below
# can write into it.
RUN chown pyktok:pyktok /app

# Switch to the unprivileged user before creating the venv, so every file
# inside the venv is user-owned from the start. No chown needed afterwards,
# no gosu needed at runtime.
USER pyktok
ENV PATH="/app/.venv/bin:$PATH"

# Create venv as pyktok — dist-info files will be user-owned at build time,
# so future `pip install --upgrade` calls (from the entrypoint, also as pyktok)
# never hit a root-owned dist-info they cannot replace.
RUN python -m venv /app/.venv

# Install deps as pyktok
COPY --chown=pyktok:pyktok requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY --chown=pyktok:pyktok main.py .
COPY --chown=pyktok:pyktok templates/ templates/
COPY --chown=pyktok:pyktok static/ static/

# data/ pre-created and owned by pyktok (we are pyktok in this RUN)
RUN mkdir -p data

# Entrypoint must be executable. --chmod=755 sets the mode at COPY time
# (a non-root user can't chmod a file they don't own, so this is required).
COPY --chown=pyktok:pyktok --chmod=755 docker-entrypoint.sh /docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/docker-entrypoint.sh"]
