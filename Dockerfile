# syntax=docker/dockerfile:1

# Matches the Python version used in CI (tests.yml).
# 3.12 is the current LTS — stable and well-tested with all dependencies.
FROM python:3.12-slim-bookworm

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

# Patch Debian packages in the base image to clear known CVEs, and install
# ca-certificates (Tailscale needs them to reach the coordination server).
# hadolint ignore=DL3005
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------
# Tailscale (for routing Instagram traffic through a home exit node)
# -----------------------------------------------------------------
# Copy the static tailscale/tailscaled binaries straight from the official
# image — no apt repo, no systemd needed. Userspace networking mode (set in
# start.sh) means the container needs no TUN device or NET_ADMIN capability,
# which is exactly what Railway's sandbox allows.
COPY --from=docker.io/tailscale/tailscale:stable /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=docker.io/tailscale/tailscale:stable /usr/local/bin/tailscale /usr/local/bin/tailscale

# -----------------------------------------------------------------
# App
# -----------------------------------------------------------------
WORKDIR /app

# Copy requirements first so Docker can cache this layer.
# If only main.py changes, pip install is skipped on the next build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot itself + the entrypoint that boots Tailscale before the bot.
COPY main.py database.py ./
COPY start.sh ./
# Strip any CRLF (repo lives on Windows) and make it executable.
RUN sed -i 's/\r$//' /app/start.sh && chmod +x /app/start.sh

# -----------------------------------------------------------------
# Runtime
# -----------------------------------------------------------------
# DATA_DIR is intentionally NOT set here — it comes from Railway's
# environment variables (set to /data, where the volume is mounted).

# No EXPOSE — this is a polling bot, not a web server.
# No HEALTHCHECK — Railway doesn't need one for worker services.

ENTRYPOINT ["/app/start.sh"]