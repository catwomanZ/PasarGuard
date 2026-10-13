#!/usr/bin/env bash
# CATWOMAN - bring up the panel, the node and nginx in one container.
set -uo pipefail

DATA_DIR="${DATA_DIR:-/var/lib/pasarguard}"
# HTTP_PORT wins if it is set; otherwise follow Railway's PORT; otherwise 8080.
# Railway routes its edge to the port it injects, so ignoring PORT here would
# leave nginx listening on a port the world is not pointed at.
HTTP_PORT="${HTTP_PORT:-${PORT:-8080}}"
PANEL_PORT="${PANEL_PORT:-8000}"
NODE_PORT="${NODE_PORT:-62050}"
NODE_API_PORT="${NODE_API_PORT:-62051}"
INBOUND_PORT="${INBOUND_PORT:-10085}"
TUNNEL_PATH="${TUNNEL_PATH:-/ws}"
BRAND="${BRAND:-CATWOMAN}"

export TZ="${TZ:-Asia/Tehran}"
mkdir -p "$DATA_DIR" "$DATA_DIR/templates/subscription" "$DATA_DIR/node" /run/nginx

log() { printf '[entrypoint] %s\n' "$*"; }
die() { printf '[entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# the subscription page template
# --------------------------------------------------------------------------
# PasarGuard reads these from disk and prefers them over its own. Copied on
# every boot so an image update also refreshes the page.
if cp -f /opt/templates/subscription/* "$DATA_DIR/templates/subscription/" 2>/dev/null; then
  log "subscription page template installed"
else
  log "WARNING: could not install the subscription page template"
fi

export CUSTOM_TEMPLATES_DIRECTORY="$DATA_DIR/templates/"
export SUBSCRIPTION_PAGE_TEMPLATE="subscription/index.html"
export SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:///${DATA_DIR}/db.sqlite3"
export UVICORN_HOST="127.0.0.1"
export UVICORN_PORT="$PANEL_PORT"
export UVICORN_PROXY_HEADERS="True"
export UVICORN_FORWARDED_ALLOW_IPS="127.0.0.1"

# --------------------------------------------------------------------------
# the node certificate
# --------------------------------------------------------------------------
# The panel validates the node's certificate, so its SAN has to match the
# address typed into the node form. Both halves run in this container and talk
# over loopback, so a fixed self-signed pair is fine -- it is generated once
# and lives on the volume.
if [ ! -f "$DATA_DIR/node/ssl_cert.pem" ]; then
  log "generating the node certificate"
  openssl req -x509 -newkey rsa:2048 \
    -keyout "$DATA_DIR/node/ssl_key.pem" \
    -out    "$DATA_DIR/node/ssl_cert.pem" \
    -days 3650 -nodes -subj "/CN=127.0.0.1" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null \
    || die "could not generate the node certificate"
fi
chmod 600 "$DATA_DIR/node/ssl_key.pem"

# The node needs its API key before it starts, and bootstrap.py later registers
# the same key with the panel. Generated once and kept on the volume.
if [ ! -s "$DATA_DIR/node/api_key" ]; then
  python -c "import secrets,uuid;print(uuid.uuid4())" > "$DATA_DIR/node/api_key"
  log "generated the node API key"
fi
chmod 600 "$DATA_DIR/node/api_key"

# --------------------------------------------------------------------------
# database migrations
# --------------------------------------------------------------------------
# The stock image did this in its own start.sh, which this entrypoint replaces.
# On a brand new volume there is no schema at all without it -- the panel comes
# up and then fails on every query, so this has to run first and has to be fatal
# if it fails.
log "running database migrations"
if ! (cd /code && python -m alembic upgrade head) >/tmp/alembic.log 2>&1; then
  tail -25 /tmp/alembic.log
  die "database migrations failed"
fi
log "database schema is up to date"

# --------------------------------------------------------------------------
# start the panel
# --------------------------------------------------------------------------
log "starting the panel on 127.0.0.1:$PANEL_PORT"
python /code/main.py >/tmp/panel.log 2>&1 &
PANEL_PID=$!

for _ in $(seq 1 90); do
  curl -fsS -o /dev/null "http://127.0.0.1:$PANEL_PORT/health" 2>/dev/null && break
  kill -0 "$PANEL_PID" 2>/dev/null || { tail -25 /tmp/panel.log; die "the panel exited on startup"; }
  sleep 1
done
curl -fsS -o /dev/null "http://127.0.0.1:$PANEL_PORT/health" 2>/dev/null || {
  tail -25 /tmp/panel.log; die "the panel never became healthy"; }
log "panel is up"

# --------------------------------------------------------------------------
# start the node
# --------------------------------------------------------------------------
NODE_ENV=(
  "API_KEY=$(cat "$DATA_DIR/node/api_key")"
  "NODE_HOST=127.0.0.1"
  "SERVICE_PORT=$NODE_PORT"
  "SERVICE_PROTOCOL=grpc"
  "SSL_CERT_FILE=$DATA_DIR/node/ssl_cert.pem"
  "SSL_KEY_FILE=$DATA_DIR/node/ssl_key.pem"
  "XRAY_EXECUTABLE_PATH=/usr/local/bin/xray"
  "XRAY_ASSETS_PATH=/usr/local/bin"
)
log "starting the node on 127.0.0.1:$NODE_PORT"
env "${NODE_ENV[@]}" /usr/local/bin/pasarguard-node >/tmp/node.log 2>&1 &
NODE_PID=$!
sleep 2
kill -0 "$NODE_PID" 2>/dev/null || { tail -20 /tmp/node.log; die "the node exited on startup"; }
log "node is up"

# --------------------------------------------------------------------------
# configure the panel through its own API
# --------------------------------------------------------------------------
log "running the bootstrap"
PANEL_URL="http://127.0.0.1:$PANEL_PORT" \
APP_DIR=/code \
PYTHON="$(command -v python)" \
DATA_DIR="$DATA_DIR" \
INBOUND_PORT="$INBOUND_PORT" \
TUNNEL_PATH="$TUNNEL_PATH" \
BRAND="$BRAND" \
python /opt/bootstrap.py || log "bootstrap reported a problem (continuing)"

# --------------------------------------------------------------------------
# nginx, last, so it never proxies to a half-started backend
# --------------------------------------------------------------------------
sed -e "s|@@HTTP_PORT@@|$HTTP_PORT|g" \
    -e "s|@@PANEL_PORT@@|$PANEL_PORT|g" \
    -e "s|@@INBOUND_PORT@@|$INBOUND_PORT|g" \
    -e "s|@@TUNNEL_PATH@@|$TUNNEL_PATH|g" \
    /opt/nginx.conf.template > /tmp/nginx.conf
nginx -t -c /tmp/nginx.conf || die "the rendered nginx config is invalid"
log "starting nginx on :$HTTP_PORT"
nginx -c /tmp/nginx.conf -g 'daemon off;' &

log "ready -- panel and node are running"

# --------------------------------------------------------------------------
# supervise: if the panel dies, the container should die so Railway restarts it
# --------------------------------------------------------------------------
while true; do
  kill -0 "$PANEL_PID" 2>/dev/null || die "the panel stopped"
  kill -0 "$NODE_PID"  2>/dev/null || die "the node stopped"
  sleep 5
done
