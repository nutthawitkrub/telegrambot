#!/usr/bin/env sh
# Railway container entrypoint.
#
# Boots Tailscale in userspace mode (no TUN device / NET_ADMIN needed inside a
# container), brings the node up, and routes ALL outbound traffic through the
# home machine acting as a Tailscale exit node. Tailscale exposes a local HTTP
# proxy on 127.0.0.1:1055; instagram_monitor is pointed at it via INSTA_PROXY_URL,
# so every Instagram request egresses from the home (residential) IP instead of
# Railway's datacenter IP — which is what clears the anonymous 401.
#
# Required env vars (set in Railway → Variables):
#   TS_AUTHKEY     - Tailscale auth key (reusable + ephemeral) from the admin console
#   TS_EXIT_NODE   - the home machine's Tailscale IP (100.x.y.z) or MagicDNS name
#   INSTA_PROXY_URL=http://127.0.0.1:1055   - tells main.py to proxy the monitor
set -e

STATE_DIR="${DATA_DIR:-/data}/ts"
mkdir -p "$STATE_DIR"

echo "[start] launching tailscaled (userspace)…"
/usr/local/bin/tailscaled \
  --tun=userspace-networking \
  --outbound-http-proxy-listen=127.0.0.1:1055 \
  --socks5-server=127.0.0.1:1056 \
  --state="$STATE_DIR/tailscaled.state" &

# tailscaled needs a moment to create its control socket before `up` can talk to
# it; retry until the node comes up or we give up after ~60s.
echo "[start] bringing tailscale up via exit-node ${TS_EXIT_NODE}…"
n=0
until /usr/local/bin/tailscale up \
        --authkey="${TS_AUTHKEY}" \
        --hostname=railway-igbot \
        --exit-node="${TS_EXIT_NODE}" \
        --accept-dns=false; do
  n=$((n + 1))
  if [ "$n" -ge 30 ]; then
    echo "[start] WARNING: tailscale up did not succeed after 60s; continuing anyway"
    break
  fi
  sleep 2
done

/usr/local/bin/tailscale status || true
echo "[start] handing off to python main.py"
exec python main.py
