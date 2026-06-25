# syntax=docker/dockerfile:1

# Pinned to match the local dev environment exactly.
# To upgrade: change the patch version here and rebuild.
FROM python:3.14.5-slim-bookworm

# -----------------------------------------------------------------
# System housekeeping
# -----------------------------------------------------------------
# Keeps Python from buffering stdout/stderr so log lines appear in
# Railway's log viewer in real time instead of in large delayed bursts.
# Force UTF-8 I/O so instagram_monitor's Unicode output (box-drawing
# lines, emoji) never triggers UnicodeEncodeError when stdout/stderr
# are redirected away from a real console (which they always are
# in a container).
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

# Patch Debian packages in the base image to clear known CVEs.
# hadolint ignore=DL3005
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------
# App
# -----------------------------------------------------------------
WORKDIR /app

# Copy requirements first so Docker can cache this layer.
# If only main.py changes, pip install is skipped on the next build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot itself.
COPY main.py database.py ./

# -----------------------------------------------------------------
# Runtime
# -----------------------------------------------------------------
# DATA_DIR is intentionally NOT set here — it comes from Railway's
# environment variables (set to /data, where the volume is mounted).

# No EXPOSE — this is a polling bot, not a web server.
# No HEALTHCHECK — Railway doesn't need one for worker services.

CMD ["python", "main.py"]