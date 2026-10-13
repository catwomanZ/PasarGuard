#!/usr/bin/env python3
"""
CATWOMAN — one-shot setup for a fresh PasarGuard install.

Runs on every boot but is written to be idempotent: each step checks whether it
has already been done and skips if so. Nothing here touches user data.

Why this file exists at all: PasarGuard's own installer is interactive (it asks
for a database, an SSL mode, an admin), and it writes a docker-compose file that
uses `network_mode: host`. Neither works on Railway, where the service gets one
port, no shell to answer prompts, and an ephemeral filesystem outside the volume.

Everything below was verified against a running PasarGuard v5.4.1 -- the field
names, the order the objects must be created in, and the error strings are all
observed, not guessed.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# configuration, all overridable from Railway Variables
# ---------------------------------------------------------------------------
PANEL = os.environ.get("PANEL_URL", "http://127.0.0.1:8000")
APP_DIR = os.environ.get("APP_DIR", "/code")
# the image puts its virtualenv first on PATH; be explicit anyway so the CLI
# subprocess can never pick up a bare system python
PYTHON = os.environ.get("PYTHON", "/code/.venv/bin/python")
DATA = Path(os.environ.get("DATA_DIR", "/var/lib/pasarguard"))

BRAND = os.environ.get("BRAND", "CATWOMAN")
SUPPORT_URL = os.environ.get("SUB_SUPPORT_URL", "https://t.me/Catwoman_ZaHra")
ANNOUNCE = os.environ.get("SUB_ANNOUNCE", "این پنل توسط Catwoman ساخته شده")

ADMIN_USER = os.environ.get("ADMIN_USERNAME", "catwoman")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "")
# PasarGuard refuses to seed an admin from env in production ("env admin not
# allowed in production"), so one is created here through the temp-key flow.
# The password must contain at least two uppercase letters -- enforced by the
# panel, and the exact error is "Password must contain at least 2 uppercase letters".

INBOUND_TAG = os.environ.get("INBOUND_TAG", "CATWOMAN-VLESS-WS")
INBOUND_PORT = int(os.environ.get("INBOUND_PORT", "10085"))
TUNNEL_PATH = os.environ.get("TUNNEL_PATH", "/ws")
CORE_NAME = os.environ.get("CORE_NAME", "catwoman-core")
NODE_NAME = os.environ.get("NODE_NAME", "catwoman-node")
GROUP_NAME = os.environ.get("GROUP_NAME", "CATWOMAN")
HOST_REMARK = os.environ.get("INBOUND_REMARK", BRAND)
NODE_CERT = DATA / "node" / "ssl_cert.pem"
NODE_KEY = DATA / "node" / "ssl_key.pem"
NODE_API_KEY_FILE = DATA / "node" / "api_key"
NODE_PORT = int(os.environ.get("NODE_PORT", "62050"))
NODE_API_PORT = int(os.environ.get("NODE_API_PORT", "62051"))


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[bootstrap] !! {msg}", flush=True)


# ---------------------------------------------------------------------------
# tiny HTTP client
# ---------------------------------------------------------------------------
def call(path, method="GET", body=None, token=None, form=False, timeout=30):
    url = PANEL + path
    data = None
    headers = {}
    if form:
        data = body.encode() if isinstance(body, str) else body
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")[:400]
    except Exception as e:  # connection refused while the panel is still starting
        return 0, str(e)


def unwrap(res, key):
    """Return the list out of a response that may or may not be wrapped.

    The list endpoints are not consistent: /api/hosts returns a bare array
    while /api/cores, /api/groups and /api/nodes each wrap theirs in an object.
    Getting this wrong is silent -- the lookup simply finds nothing and creates
    a duplicate.
    """
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        v = res.get(key)
        if isinstance(v, list):
            return v
    return None


def wait_for_panel(seconds=180):
    log(f"waiting for the panel at {PANEL} ...")
    deadline = time.time() + seconds
    while time.time() < deadline:
        code, _ = call("/health", timeout=5)
        if code == 200:
            log("panel is up")
            return True
        time.sleep(2)
    warn("panel did not come up in time")
    return False


# ---------------------------------------------------------------------------
# step 1 - the owner account
# ---------------------------------------------------------------------------
def panel_prefix() -> str:
    return os.environ.get("DASHBOARD_PATH", "/dashboard/")


def create_owner():
    """Create the owner using the one-time key flow. Returns True on success."""
    log("no owner yet -- creating one")

    env = dict(os.environ)
    env.setdefault("SQLALCHEMY_DATABASE_URL", f"sqlite+aiosqlite:///{DATA}/db.sqlite3")
    out = subprocess.run(
        [PYTHON, "-m", "cli.main", "generate-temp-key"],
        capture_output=True, text=True, cwd=APP_DIR, env=env, timeout=120,
    )
    key = None
    for line in (out.stdout or "").splitlines():
        if "Temp key:" in line:
            key = line.split("Temp key:")[-1].strip()
    if not key:
        warn("could not generate a temp key")
        warn((out.stdout or "")[-300:] + (out.stderr or "")[-300:])
        return False

    password = ADMIN_PASS
    if not password:
        # generate something that satisfies "at least 2 uppercase letters"
        password = "Cw" + secrets.token_urlsafe(14) + "9"
        warn(f"no ADMIN_PASSWORD set; using a generated one: {password}")

    code, res = call("/api/setup/owner", "POST",
                     {"key": key, "username": ADMIN_USER, "password": password})
    if code == 201:
        log(f"owner '{ADMIN_USER}' created")
        return True
    if code == 409:
        log("owner already exists")
        return True
    warn(f"owner creation failed: HTTP {code} {res}")
    return False


def login() -> str | None:
    if not ADMIN_PASS:
        warn("no ADMIN_PASSWORD set and the owner already exists -- cannot log in to bootstrap")
        return None
    code, res = call("/api/admin/token", "POST",
                     f"username={ADMIN_USER}&password={ADMIN_PASS}", form=True)
    if code == 200 and isinstance(res, dict) and res.get("access_token"):
        log(f"logged in as {ADMIN_USER}")
        return res["access_token"]
    warn(f"login failed: HTTP {code} {res}")
    return None


# ---------------------------------------------------------------------------
# step 2 - the Xray core
# ---------------------------------------------------------------------------
def ensure_core(token) -> int | None:
    code, res = call("/api/cores", token=token)
    cores = unwrap(res, "cores")
    if cores is not None:
        for c in cores:
            if c.get("name") == CORE_NAME:
                log(f"core already present (id {c['id']})")
                return c["id"]

    log("creating the Xray core")
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": INBOUND_TAG,
            "listen": "127.0.0.1",
            "port": INBOUND_PORT,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {"network": "ws", "wsSettings": {"path": TUNNEL_PATH}},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        }],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    }
    code, res = call("/api/core", "POST",
                     {"name": CORE_NAME, "type": "xray", "config": config}, token=token)
    if code == 201:
        log(f"core created (id {res['id']}), inbound tag {INBOUND_TAG}")
        return res["id"]
    warn(f"core creation failed: HTTP {code} {res}")
    return None


# ---------------------------------------------------------------------------
# step 3 - the node
# ---------------------------------------------------------------------------
def ensure_node_cert():
    """A self-signed cert whose SAN matches the address the panel will dial.

    The panel validates the node's certificate, so the CN/SAN must equal
    exactly the address typed into the node form -- here, loopback.
    """
    NODE_CERT.parent.mkdir(parents=True, exist_ok=True)
    if NODE_CERT.exists() and NODE_KEY.exists():
        log("node certificate already present")
    else:
        log("generating the node certificate")
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(NODE_KEY), "-out", str(NODE_CERT),
            "-days", "3650", "-nodes", "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ], check=True, capture_output=True)
    NODE_KEY.chmod(0o600)

    if NODE_API_KEY_FILE.exists():
        api_key = NODE_API_KEY_FILE.read_text().strip()
    else:
        api_key = secrets.token_hex(16)
        api_key = f"{api_key[:8]}-{api_key[8:12]}-{api_key[12:16]}-{api_key[16:20]}-{api_key[20:32]}"
        NODE_API_KEY_FILE.write_text(api_key)
        NODE_API_KEY_FILE.chmod(0o600)
    return api_key


def ensure_node(token, core_id) -> int | None:
    code, res = call("/api/nodes", token=token)
    nodes = unwrap(res, "nodes")
    if nodes is not None:
        for n in nodes:
            if n.get("name") == NODE_NAME:
                log(f"node already registered (id {n['id']}, status {n.get('status')})")
                return n["id"]

    api_key = ensure_node_cert()
    ca = NODE_CERT.read_text()
    log("registering the node")
    code, res = call("/api/node", "POST", {
        "name": NODE_NAME,
        "address": "127.0.0.1",
        "port": NODE_PORT,
        "api_port": NODE_API_PORT,
        "connection_type": "grpc",
        "core_config_id": core_id,
        "keep_alive": 10,
        "server_ca": ca,
        "api_key": api_key,
    }, token=token)
    if code in (200, 201):
        log(f"node registered (id {res['id']})")
        return res["id"]
    warn(f"node registration failed: HTTP {code} {res}")
    return None


def wait_for_node(token, node_id, seconds=90) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        code, n = call(f"/api/node/{node_id}", token=token, timeout=10)
        if isinstance(n, dict) and n.get("status") == "connected":
            log(f"node connected, xray {n.get('xray_version')}")
            return True
        time.sleep(3)
    warn("node did not report 'connected' in time")
    return False


# ---------------------------------------------------------------------------
# step 4 - host, group
# ---------------------------------------------------------------------------
def public_host() -> str:
    host = os.environ.get("PUBLIC_HOST", "").strip()
    if host:
        return host
    # Railway sets this for any service that has a public domain attached
    return os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()


def ensure_host(token) -> int | None:
    host = public_host()
    if not host:
        warn("PUBLIC_HOST is empty -- the share link will carry a placeholder address")
        warn("run `railway domain` (or Generate Domain in the dashboard) and redeploy")
        host = "CHANGE-ME.up.railway.app"

    code, res = call("/api/hosts", token=token)
    hosts = unwrap(res, "hosts")
    if hosts is not None:
        for h in hosts:
            if h.get("remark") == HOST_REMARK and h.get("inbound_tag") == INBOUND_TAG:
                log(f"host already present (id {h['id']}) -> {host}")
                return h["id"]

    log(f"creating the host for {host}")
    code, res = call("/api/host/", "POST", {
        "remark": HOST_REMARK,
        "address": [host],
        "port": int(os.environ.get("PUBLIC_PORT", "443")),
        "inbound_tag": INBOUND_TAG,
        "path": TUNNEL_PATH,
        "security": "tls",
        "priority": 1,
        "is_disabled": False,
        "random_user_agent": False,
        "use_sni_as_host": False,
    }, token=token)
    if code in (200, 201):
        log(f"host created (id {res['id']})")
        return res["id"]
    warn(f"host creation failed: HTTP {code} {res}")
    return None


def ensure_group(token) -> int | None:
    code, res = call("/api/groups", token=token)
    groups = unwrap(res, "groups")
    if groups is not None:
        for g in groups:
            if g.get("name") == GROUP_NAME:
                log(f"group already present (id {g['id']})")
                return g["id"]

    log("creating the group")
    # Without a group a user is not attached to any inbound, and PasarGuard
    # silently returns an EMPTY subscription -- no error, just no configs.
    code, res = call("/api/group", "POST", {
        "name": GROUP_NAME,
        "inbound_tags": [INBOUND_TAG],
        "is_disabled": False,
    }, token=token)
    if code in (200, 201):
        log(f"group created (id {res['id']})")
        return res["id"]
    warn(f"group creation failed: HTTP {code} {res}")
    return None


# ---------------------------------------------------------------------------
# step 5 - branding
# ---------------------------------------------------------------------------
def ensure_branding(token):
    code, settings = call("/api/settings", token=token)
    if code != 200 or not isinstance(settings, dict):
        warn(f"could not read settings: HTTP {code}")
        return
    sub = settings.get("subscription") or {}
    wanted = {
        "profile_title": BRAND,
        "support_url": SUPPORT_URL,
        "announce": ANNOUNCE,
    }
    changes = {k: v for k, v in wanted.items() if sub.get(k) != v}
    if not changes:
        log("branding already set")
        return
    sub.update(wanted)
    code, res = call("/api/settings", "PUT", {"subscription": sub}, token=token)
    if code == 200:
        log(f"branding set: {', '.join(changes)}")
    else:
        warn(f"branding update failed: HTTP {code} {res}")


# ---------------------------------------------------------------------------
def main() -> int:
    if not wait_for_panel():
        return 1

    # Log in first. A healthy install answers on the first try and no temp key
    # is ever minted. Only a genuinely fresh database falls through to setup.
    token = login()
    if not token:
        log("login failed -- assuming this is a fresh install")
        if not create_owner():
            return 1
        token = login()
    if not token:
        # Owner exists but the password differs from ADMIN_PASSWORD: the panel
        # is fine, this boot simply has nothing to configure.
        warn("skipping API bootstrap (cannot log in)")
        return 0

    core_id = ensure_core(token)
    if core_id is None:
        return 1
    node_id = ensure_node(token, core_id)
    if node_id is not None:
        wait_for_node(token, node_id)
    ensure_host(token)
    ensure_group(token)
    ensure_branding(token)

    log("bootstrap complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
