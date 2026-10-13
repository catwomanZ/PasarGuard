#!/usr/bin/env python3
"""CATWOMAN - learn this container's own public address from real traffic.

Why this exists
---------------
The panel builds every config out of the address stored in its host record, and
nothing ever tells it what address it is actually reached on. That address has
to be seeded from somewhere -- an environment variable, or RAILWAY_PUBLIC_DOMAIN
-- and both of those can be empty, and both can go stale. When either happens
the symptom is the worst kind:

    the subscription page keeps working, because that page builds its own URL
    out of the address in the browser, while every single config it hands out
    points at an address nobody answers on.

So this learns the address the honest way: from the traffic. nginx writes the
Host header of every request the panel receives into a small file. Whatever
address real people actually reach this panel on is, by definition, the address
that works. When it disagrees with the panel's host record, the record is
corrected in place and the log says so.

The upshot: a regenerated domain, a never-set domain, a placeholder -- the first
visit fixes it, with no variable to set and nothing to remember.
"""

from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bootstrap  # noqa: E402  (same directory, deliberately shared logic)

HOST_LOG = os.environ.get("HOST_LOG", "/tmp/cw_host.log")
INTERVAL = float(os.environ.get("AUTOHOST_INTERVAL", "2"))
STATE = os.environ.get("AUTOHOST_STATE", "/tmp/cw_host_applied")

# A hostname worth trusting: at least one dot, letters/digits/hyphens, no
# underscore, no whitespace, and not an IP literal. Anything that fails this is
# a scanner, a health check, or an internal address, and is ignored.
GOOD = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")
IP = re.compile(r"^[0-9.]+$")

# Addresses that are real but are never right for a config.
NEVER = {
    "localhost",
    "healthcheck.railway.app",
    "127.0.0.1",
    "0.0.0.0",
}


def usable(host: str) -> bool:
    h = (host or "").strip().lower()
    # DNS is case-insensitive, so the placeholders have to be compared lowered --
    # otherwise 'CHANGE-ME.up.railway.app' sails through as a real address.
    if not h or h in NEVER or h in {p.lower() for p in bootstrap.PLACEHOLDERS}:
        return False
    if IP.match(h) or ":" in h or "_" in h:
        return False
    return bool(GOOD.match(h))


def drain() -> list[str]:
    """Every host written since the last call, then empty the file.

    Truncated in place rather than deleted on purpose: nginx holds the file open
    and keeps writing to the same inode, so swapping the file out from under it
    would silently send every later request into a file nobody is reading.
    """
    try:
        with open(HOST_LOG, "r+", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
            fh.seek(0)
            fh.truncate(0)
    except FileNotFoundError:
        return []
    except OSError as e:
        print(f"[autohost] cannot read {HOST_LOG}: {e}", flush=True)
        return []
    return [line.strip() for line in data.splitlines() if line.strip()]


def applied() -> str:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def remember(host: str) -> None:
    try:
        with open(STATE, "w", encoding="utf-8") as fh:
            fh.write(host)
    except OSError:
        pass


def main() -> int:
    bootstrap.log(
        "watching for the address this panel is really reached on "
        f"(from {HOST_LOG})"
    )
    counts: dict[str, int] = {}
    last_attempt = 0.0
    token = None

    while True:
        for host in drain():
            if usable(host):
                counts[host.lower()] = counts.get(host.lower(), 0) + 1
            elif host:
                once = f"ignored:{host.lower()}"
                if once not in counts:
                    counts[once] = 1

        if counts:
            # The most-seen host wins, so a stray scan cannot outvote the person
            # actually using the panel.
            best = max(counts.items(), key=lambda kv: kv[1])[0]
            if best.startswith("ignored:"):
                best = ""
        else:
            best = ""

        if best and best != applied():
            now = time.time()
            # A failed login is retried at most once a minute: a changed admin
            # password would otherwise spin a request a second forever.
            if token is None and now - last_attempt < 60:
                time.sleep(INTERVAL)
                continue
            last_attempt = now

            if token is None:
                token = bootstrap.login()
                if token is None:
                    bootstrap.warn(
                        "cannot log in to fix the host record -- if you changed the "
                        "admin password, set PUBLIC_HOST in Railway's variables to "
                        f"'{best}' and redeploy instead"
                    )
                    time.sleep(INTERVAL)
                    continue

            bootstrap.log(f"this panel is being reached on '{best}' -- checking the host record")
            # Ensure the whole bootstrap agrees, not just this module: it reads
            # these through public_host(), and RAILWAY_PUBLIC_DOMAIN is checked
            # first, so both are set to the address that demonstrably works.
            os.environ["PUBLIC_HOST"] = best
            os.environ["RAILWAY_PUBLIC_DOMAIN"] = best
            try:
                bootstrap.ensure_host(token)
                remember(best)
            except Exception as e:  # never let this die on one bad round
                bootstrap.warn(f"could not update the host record: {type(e).__name__}: {e}")
                token = None  # the token may simply have expired

        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"[autohost] stopped: {type(e).__name__}: {e}", flush=True)
        sys.exit(0)
