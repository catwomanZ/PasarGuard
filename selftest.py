#!/usr/bin/env python3
"""CATWOMAN - let the container prove its own tunnel, at every boot.

Why this exists
---------------
The panel being reachable and the panel handing out a well formed link prove
nothing about whether that link carries traffic. Those two things were true
while every config on the panel was dead, and the only way to tell the
difference was to import the link into a client on a phone and watch it fail.

This script closes that gap. It asks the panel for a real config, the same
bytes a phone would download, and then drives that config through the same
path a phone's traffic takes once it reaches us:

    xray client -> nginx (plain HTTP, Host: <public domain>) -> xray inbound
                -> freedom outbound -> the internet

The TLS hop that a phone makes (443, SNI = the public domain) ends at the
Railway edge, not in this container, so on this side of the edge the hop is
plain HTTP. That is not a shortcut, it is exactly what the edge hands us.

It prints one line per check, prefixed [selftest], straight into the deploy
log. Nobody has to trust a screenshot of a working phone.

It is a diagnostic and nothing else: every failure is reported and swallowed,
the temporary user it makes is always deleted, and it never blocks startup.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PANEL_URL = os.environ.get("PANEL_URL", "http://127.0.0.1:8000").rstrip("/")
HTTP_PORT = os.environ.get("HTTP_PORT") or os.environ.get("PORT") or "8080"
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
TUNNEL_PATH = os.environ.get("TUNNEL_PATH", "/ws")
PUBLIC_HOST = (
    os.environ.get("PUBLIC_HOST") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or ""
).strip()
TARGET = os.environ.get("SELFTEST_TARGET", "https://www.gstatic.com/generate_204")
SOCKS_PORT = int(os.environ.get("SELFTEST_SOCKS_PORT", "10877"))
TEST_USERNAME = os.environ.get("SELFTEST_USERNAME", "catwoman-selftest")
XRAY_BIN = os.environ.get("XRAY_EXECUTABLE_PATH", "/usr/local/bin/xray")
UA = "v2rayNG/1.8.5"  # the panel only renders client configs for a known client
BUDGET = int(os.environ.get("SELFTEST_BUDGET", "150"))  # seconds, whole script
OK_CODES = {"200", "204", "301", "302", "307", "308"}


def log(msg: str) -> None:
    print(f"[selftest] {msg}", flush=True)


def call(
    path: str, token: str | None = None, method: str = "GET", body: dict | None = None
) -> tuple[int, object]:
    """One panel API call. Never raises: the caller decides what a failure means."""
    url = PANEL_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw or b"null")
            except ValueError:
                return r.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")
    except Exception as e:  # DNS, refused, timeout
        return 0, str(e)


def login() -> str | None:
    # the token endpoint wants a form, not JSON
    data = urllib.parse.urlencode(
        {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    ).encode()
    req = urllib.request.Request(
        PANEL_URL + "/api/admin/token", data=data, method="POST"
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return (json.loads(r.read()) or {}).get("access_token")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:120]
        log(f"panel login refused (HTTP {e.code}) {detail}")
    except Exception as e:
        log(f"panel login failed: {e}")
    return None


def unwrap(res: object, key: str):
    if isinstance(res, dict) and key in res:
        return res[key]
    return res


def find_group(token: str) -> int | None:
    code, res = call("/api/groups", token=token)
    groups = unwrap(res, "groups") if code == 200 else None
    if not isinstance(groups, list):
        return None
    ours = [
        g
        for g in groups
        if any(
            "CATWOMAN" in str(t).upper() or "VLESS" in str(t).upper()
            for t in (g.get("inbound_tags") or [])
        )
    ]
    pick = (ours or groups or [None])[0]
    return pick.get("id") if isinstance(pick, dict) else None


def ensure_user(token: str) -> str | None:
    """Reuse the test user if it survived a crash, otherwise make it."""
    code, _ = call(f"/api/user/{TEST_USERNAME}", token=token)
    if code == 200:
        return TEST_USERNAME
    gid = find_group(token)
    if gid is None:
        log("could not find a group to attach the test user to")
        return None
    body = {
        "username": TEST_USERNAME,
        "data_limit": 0,
        "expire": None,
        "status": "active",
        "group_ids": [gid],
        "note": "temporary -- created by the container self test",
    }
    code, res = call("/api/user", token=token, method="POST", body=body)
    if code in (200, 201):
        return TEST_USERNAME
    log(f"could not create the test user: HTTP {code} {str(res)[:160]}")
    return None


def drop_user(token: str) -> None:
    code, _ = call(f"/api/user/{TEST_USERNAME}", token=token, method="DELETE")
    if code in (200, 204):
        log("test user removed")
    else:
        log(f"could not remove the test user (HTTP {code}) -- it will be reused next boot")


def fetch_link(token: str, username: str) -> str | None:
    """The exact bytes a phone downloads, straight out of the panel."""
    code, res = call(f"/api/user/{username}", token=token)
    if code != 200 or not isinstance(res, dict):
        log(f"could not read the test user back: HTTP {code}")
        return None
    sub = res.get("subscription_url") or ""
    if not sub:
        log("the panel did not return a subscription url for the test user")
        return None
    if sub.startswith("http"):
        url = sub
    else:
        url = PANEL_URL + ("" if sub.startswith("/") else "/") + sub
    req = urllib.request.Request(url + "/links_base64")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace").strip()
    except Exception as e:
        log(f"could not download the subscription: {e}")
        return None
    try:
        raw = raw.replace("-", "+").replace("_", "/")
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode(
            "utf-8", "replace"
        )
    except Exception as e:
        log(f"the subscription body was not base64: {e}")
        return None
    for line in decoded.splitlines():
        line = line.strip()
        if line.startswith("vless://"):
            return line
    log("the subscription carried no vless config")
    return None


def client_config(link: str, path: str) -> tuple[dict, str] | None:
    """Turn the panel's own link into an Xray client that can only use the tunnel.

    There is deliberately no freedom outbound: if the request comes back at all,
    it came back through the tunnel, and there is no route by which a leak could
    be mistaken for a success.
    """
    u = urllib.parse.urlparse(link)
    q = urllib.parse.parse_qs(u.query)

    def one(key: str, default: str = "") -> str:
        vals = q.get(key) or []
        return vals[0] if vals else default

    uuid = u.username or ""
    if not uuid:
        return None
    path = one("path", TUNNEL_PATH)
    # Xray clients default the Host header to the address in the link when the
    # link has no host= parameter, so that is what we send too.
    host_header = one("host") or one("sni") or PUBLIC_HOST or (u.hostname or "")
    log(
        "link from the panel: host=%s sni=%s path=%s"
        % (one("host") or "(missing)", one("sni") or "(missing)", path)
    )
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "selftest-in",
                "listen": "127.0.0.1",
                "port": SOCKS_PORT,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": False},
            }
        ],
        "outbounds": [
            {
                "tag": "selftest-out",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            # the edge, as far as we are concerned, is nginx
                            "address": "127.0.0.1",
                            "port": int(HTTP_PORT),
                            "users": [
                                {"id": uuid, "encryption": "none", "level": 0}
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",  # TLS ends at the Railway edge
                    "wsSettings": {"path": path, "host": host_header},
                },
            }
        ],
    }
    return cfg, host_header


def port_open(port: int, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def curl(url: str, proxy: str | None = None, timeout: int = 25) -> str:
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", str(timeout)]
    if proxy:
        cmd += ["-x", proxy]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return (out.stdout or "000").strip()
    except Exception:
        return "000"


def main() -> int:
    log(f"panel {PANEL_URL}, tunnel on 127.0.0.1:{HTTP_PORT}{TUNNEL_PATH}")
    if not os.path.exists(XRAY_BIN):
        log(f"no xray core at {XRAY_BIN} -- skipping")
        return 0

    # 1. can this container reach the internet at all? Without this every
    #    tunnel failure below would be blamed on the tunnel.
    direct = curl(TARGET, timeout=20)
    if direct in OK_CODES:
        log(f"direct egress OK ({direct})")
    else:
        log(f"direct egress FAILED (curl said {direct}) -- the container itself has no internet")
        log("nothing below this line can pass, stopping here")
        return 0

    token = login()
    if not token:
        log("skipped: could not log into the panel (password changed?)")
        return 0
    username = ensure_user(token)
    if not username:
        return 0

    tmpdir = tempfile.mkdtemp(prefix="catwoman-selftest-")
    proc = None
    try:
        link = fetch_link(token, username)
        if not link:
            return 0
        built = client_config(link, tmpdir)
        if not built:
            log("the panel's own config had no uuid in it")
            return 0
        cfg, host_header = built
        log(f"connecting to 127.0.0.1:{HTTP_PORT} with Host: {host_header or '(none)'}")
        cfg_path = os.path.join(tmpdir, "client.json")
        with open(cfg_path, "w") as fh:
            json.dump(cfg, fh, indent=1)

        proc = subprocess.Popen(
            [XRAY_BIN, "run", "-c", cfg_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if not port_open(SOCKS_PORT):
            err = b""
            try:
                err = proc.stderr.read() or b""
            except Exception:
                pass
            log(f"the client core did not come up: {err.decode('utf-8','replace')[:300]}")
            return 0

        started = time.time()
        code = "000"
        # The node picks the new user up over its own channel, and nginx came up
        # seconds ago. Either can be a beat behind the panel's API, and a single
        # early failure would be reported as a dead tunnel. So: try a few times
        # before believing it, and say how many tries it took.
        for attempt in range(1, 5):
            code = curl(TARGET, proxy=f"socks5h://127.0.0.1:{SOCKS_PORT}", timeout=30)
            if code in OK_CODES:
                break
            if attempt < 4:
                time.sleep(4)
        took = time.time() - started
        if code in OK_CODES:
            log(f"TUNNEL OK -- fetched {TARGET} through {TUNNEL_PATH} in {took:.1f}s (try {attempt})")
            log("the panel's config carries traffic; anything left is the edge or the client")
        else:
            log(f"TUNNEL FAILED -- curl said {code} after {took:.1f}s and {attempt} tries")
            log(f"  the client reached 127.0.0.1:{HTTP_PORT}{TUNNEL_PATH} and got nothing back")
            log("  look at nginx /ws routing and the xray inbound before blaming the phone")
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        drop_user(token)
        try:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # a diagnostic must never take the container down
        log(f"self test crashed and was ignored: {type(e).__name__}: {e}")
        sys.exit(0)
