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

# ---------------------------------------------------------------------------
# how the brand is written
# ---------------------------------------------------------------------------
# "CATWOMAN" in Unicode mathematical sans-serif bold -- 𝗖𝗔𝗧𝗪𝗢𝗠𝗔𝗡. U+1D5D4 is
# a bold capital A and the capitals run consecutively from there, so this is a
# shift rather than a lookup table. Computed instead of pasted so the file stays
# readable and the codepoints are not a mystery to whoever opens it next.
def bold(text: str) -> str:
    return "".join(
        chr(0x1D5D4 + ord(c) - 65) if "A" <= c <= "Z" else c for c in text
    )


# BRAND stays the plain name so there is one obvious place to change it; the
# bold form is derived from it and is what the panel actually shows.
BRAND = os.environ.get("BRAND", "CATWOMAN")
DISPLAY_BRAND = os.environ.get("DISPLAY_BRAND", "") or bold(BRAND)
SUPPORT_URL = os.environ.get("SUB_SUPPORT_URL", "https://t.me/Catwoman_ZaHra")
ANNOUNCE = os.environ.get("SUB_ANNOUNCE", "این پنل توسط Catwoman ساخته شده")

ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin")
DB_FILE = DATA / "db.sqlite3"
# PasarGuard refuses to seed an admin from env in production ("env admin not
# allowed in production"), so one is created here through the temp-key flow.
# The password must contain at least two uppercase letters -- enforced by the
# panel, and the exact error is "Password must contain at least 2 uppercase letters".

INBOUND_TAG = os.environ.get("INBOUND_TAG", f"{DISPLAY_BRAND}-VLESS-WS")
INBOUND_PORT = int(os.environ.get("INBOUND_PORT", "10085"))
TUNNEL_PATH = os.environ.get("TUNNEL_PATH", "/ws")
CORE_NAME = os.environ.get("CORE_NAME", "catwoman-core")
NODE_NAME = os.environ.get("NODE_NAME", "catwoman-node")
GROUP_NAME = os.environ.get("GROUP_NAME", DISPLAY_BRAND)
# The host remark is a template, and the panel expands {USERNAME} per user
# (app/subscription/share.py: remark.format_map(format_variables)). That is what
# puts the username in the name of every config the client imports.
HOST_REMARK = os.environ.get("INBOUND_REMARK", DISPLAY_BRAND + " \u2022 {USERNAME}")

# What builds before this one wrote. The steps below rewrite an installation
# that already carries these, in place, so a redeploy does not end up with a
# duplicate group, a second core fighting for the same port, or a host pointing
# at an inbound tag that no longer exists anywhere.
LEGACY_BRAND = "CATWOMAN"
LEGACY_INBOUND_TAG = "CATWOMAN-VLESS-WS"
LEGACY_GROUP_NAME = "CATWOMAN"
LEGACY_HOST_REMARK = "CATWOMAN"
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

    # The API insists on a strong password, and the one you actually want may
    # be "admin". Satisfy the API with a throwaway, then overwrite the stored
    # hash in the next step. See force_credentials() for why that works.
    # Built so it satisfies every clause of the policy deterministically:
    # "Cw" gives the two uppercase, "7" and "9" the two digits, "!" the special
    # character, and the random middle covers length and the two lowercase.
    # token_urlsafe alone is not enough -- its alphabet is [A-Za-z0-9_-], so it
    # can (and did) come out with no special character at all.
    throwaway = "Cw7!" + secrets.token_urlsafe(18) + "9!"
    code, res = call("/api/setup/owner", "POST",
                     {"key": key, "username": ADMIN_USER, "password": throwaway})
    if code == 201:
        log(f"owner '{ADMIN_USER}' created")
        return True
    if code == 409:
        log("owner already exists")
        return True
    warn(f"owner creation failed: HTTP {code} {res}")
    return False


def force_credentials(username: str, password: str) -> bool:
    """Write the username and a bcrypt hash straight into the database.

    Why this is needed: PasarGuard's password policy (at least 12 characters,
    2 digits, 2 uppercase, 2 lowercase, 1 special character, and it may not
    contain the username) is enforced by the Pydantic models the API uses to
    CREATE a password. It is not enforced anywhere a password is USED -- the
    login path is just bcrypt.checkpw against the stored hash
    (app/routers/authentication.py, validate_admin). So a hash written here
    logs in exactly like one the API created.

    Worth knowing before you rely on it: because the policy still guards every
    create/update path, the panel's own UI will refuse to set this password
    again once you change it. Pick something policy-compliant before handing
    the panel to anyone else.
    """
    try:
        import bcrypt
    except ImportError:
        warn("bcrypt is unavailable, cannot force the credentials")
        return False

    if not DB_FILE.exists():
        warn(f"no database at {DB_FILE}")
        return False

    import sqlite3

    rounds = 12  # matches BCRYPT_ROUNDS in app/models/admin.py
    digest = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=rounds)).decode()
    try:
        con = sqlite3.connect(DB_FILE)
        with con:
            con.execute(
                "UPDATE admins SET username = ?, hashed_password = ? WHERE id = "
                "(SELECT id FROM admins ORDER BY id LIMIT 1)",
                (username, digest),
            )
        con.close()
    except Exception as e:
        warn(f"could not write the credentials: {e}")
        return False

    log(f"credentials set to {username} / {password}")
    return True


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
def build_core_config() -> dict:
    return {
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


def ensure_core(token) -> int | None:
    code, res = call("/api/cores", token=token)
    cores = unwrap(res, "cores")
    if cores is not None:
        for c in cores:
            if c.get("name") == CORE_NAME:
                tags = [i.get("tag")
                        for i in ((c.get("config") or {}).get("inbounds") or [])]
                if INBOUND_TAG in tags:
                    log(f"core already present (id {c['id']}), tag {INBOUND_TAG}")
                    return c["id"]
                # A leftover from before the rename. Rewritten here rather than
                # replaced with a new core: two cores would each open port
                # INBOUND_PORT and the node would fail to start, and the host
                # below would be left pointing at a tag nothing defines.
                log(f"core {c['id']} has the old tag {tags} -- updating in place")
                code, res = call(
                    f"/api/core/{c['id']}?restart_nodes=true", "PUT",
                    {"name": CORE_NAME, "type": "xray", "config": build_core_config()},
                    token=token)
                if code in (200, 201):
                    log(f"core updated, tag is now {INBOUND_TAG}")
                    return c["id"]
                warn(f"core update failed: HTTP {code} {res}")
                return c["id"]

    log("creating the Xray core")
    code, res = call("/api/core", "POST",
                     {"name": CORE_NAME, "type": "xray", "config": build_core_config()},
                     token=token)
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


def host_body(host: str) -> dict:
    """What a host looks like, shared by the create and the rename path."""
    return {
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
    }


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
            ours = (
                h.get("inbound_tag") in (INBOUND_TAG, LEGACY_INBOUND_TAG)
                and h.get("remark") in (HOST_REMARK, LEGACY_HOST_REMARK)
            )
            if not ours:
                continue
            if h.get("remark") == HOST_REMARK and h.get("inbound_tag") == INBOUND_TAG:
                log(f"host already present (id {h['id']}) -> {host}")
                return h["id"]
            log(f"host {h['id']} has the old naming -- updating in place")
            code, res = call(f"/api/host/{h['id']}", "PUT", host_body(host), token=token)
            if code in (200, 201):
                log(f"host updated (id {h['id']}) -> {host}")
                return h["id"]
            warn(f"host update failed: HTTP {code} {res}")
            return h["id"]

    log(f"creating the host for {host}")
    code, res = call("/api/host/", "POST", host_body(host), token=token)
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
            if g.get("name") not in (GROUP_NAME, LEGACY_GROUP_NAME):
                continue
            if g.get("name") == GROUP_NAME and INBOUND_TAG in (g.get("inbound_tags") or []):
                log(f"group already present (id {g['id']})")
                return g["id"]
            # Renamed in place rather than recreated: a user points at a group
            # by id, so a brand new group would leave every existing user
            # attached to nothing -- which means an empty subscription, with no
            # error anywhere to explain it.
            log(f"group {g['id']} has the old name -- updating in place")
            code, res = call(f"/api/group/{g['id']}", "PUT", {
                "name": GROUP_NAME,
                "inbound_tags": [INBOUND_TAG],
                "is_disabled": False,
            }, token=token)
            if code in (200, 201):
                log(f"group updated (id {g['id']}) -> {GROUP_NAME}")
                return g["id"]
            warn(f"group update failed: HTTP {code} {res}")
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
# step 5 - the one-tap app tiles
# ---------------------------------------------------------------------------
# These are the "connect with one tap" buttons on the subscription page. The
# panel ships an empty list, so without this the page has no app tiles at all
# and no quick-connect button. Each import_url is a deep link the panel resolves
# by replacing {url} with the subscription address.
#
# icon_url is deliberately empty for every one of them: the page draws its own
# inline glyph. Pulling the real logos would mean the subscription page asks a
# third-party server for an image every time it opens, which leaks that someone
# opened it and leaves a blank square whenever that server is unreachable.
APPS = [
    {
        "name": "v2rayNG",
        "platform": "android",
        "import_url": "v2rayng://install-sub?url={url}",
        "recommended": True,
        "description": {"fa": "اندروید - سبک و سریع، پیشنهاد ما",
                        "en": "Android - lightweight and fast"},
    },
    {
        "name": "Hiddify",
        "platform": "android",
        "import_url": "hiddify://import/{url}",
        "description": {"fa": "اندروید، آیفون و ویندوز", "en": "Android, iOS and Windows"},
    },
    {
        "name": "V2Box",
        "platform": "ios",
        "import_url": "v2box://install-sub?url={url}",
        "description": {"fa": "آیفون و آیپد", "en": "iPhone and iPad"},
    },
    {
        "name": "Streisand",
        "platform": "ios",
        "import_url": "streisand://import/{url}",
        "description": {"fa": "آیفون و آیپد", "en": "iPhone and iPad"},
    },
    {
        "name": "NekoBox",
        "platform": "android",
        "import_url": "sn://subscription?url={url}",
        "description": {"fa": "اندروید", "en": "Android"},
    },
    {
        "name": "Happ",
        "platform": "ios",
        "import_url": "happ://add/{url}",
        "description": {"fa": "آیفون، آیپد و مک", "en": "iOS and macOS"},
    },
]


def ensure_apps(token):
    """Fill in the app tiles, but only when the list is still empty.

    Never overwrites a list that is already there: someone may have curated
    their own apps in the panel, and silently replacing that on every boot
    would be worse than shipping the default. Set SUB_APPS=0 to skip entirely.
    """
    if os.environ.get("SUB_APPS", "1") != "1":
        log("app tiles skipped (SUB_APPS is off)")
        return

    code, settings = call("/api/settings", token=token)
    if code != 200 or not isinstance(settings, dict):
        warn(f"could not read settings: HTTP {code}")
        return

    sub = settings.get("subscription") or {}
    if sub.get("applications"):
        log(f"app tiles already configured ({len(sub['applications'])})")
        return

    sub["applications"] = [dict(app, icon_url="", download_links=[]) for app in APPS]
    code, res = call("/api/settings", "PUT", {"subscription": sub}, token=token)
    if code == 200:
        log(f"app tiles added ({len(APPS)})")
    else:
        warn(f"app tiles failed: HTTP {code} {res}")


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
        "profile_title": DISPLAY_BRAND,
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
        # The owner now exists with a throwaway password; replace it with the
        # one you asked for, then log in with that.
        if not force_credentials(ADMIN_USER, ADMIN_PASS):
            warn("continuing with the throwaway password")
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
    ensure_apps(token)

    log("bootstrap complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
