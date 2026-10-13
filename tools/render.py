#!/usr/bin/env python3
"""Render the subscription template outside the panel.

Drives the real template through the same Jinja environment the panel builds
(app/templates/__init__.py: FileSystemLoader over the template directories, the
same two custom filters, `now` as a global, autoescape OFF) so a layout change
can be checked without booting PasarGuard, a node and Xray just to look at a
page.

    python3 render.py /tmp/out.html [state]

`state` is one of normal / heavy / unlimited / expired, and only changes the
numbers handed to the template.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

HERE = Path(__file__).resolve().parent.parent          # pgship/
TEMPLATES = HERE / "templates"


def bytesformat(n, *a, **k):
    """Stand-in for the panel's readable_size filter."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def datetimeformat(value, fmt="%Y-%m-%d %H:%M:%S"):
    if value is None:
        return ""
    return value.strftime(fmt)


def build(state):
    now = datetime.now(timezone.utc)
    limit = 10 * 1024 ** 3
    expire = now + timedelta(days=35)
    used = 3.7 * 1024 ** 3
    if state == "heavy":
        used = 9.5 * 1024 ** 3
        expire = now + timedelta(days=12)
    elif state == "unlimited":
        limit, used = 0, 2 * 1024 ** 3
        expire = None
    elif state == "expired":
        used = 10 * 1024 ** 3
        expire = now - timedelta(days=3)

    user = SimpleNamespace(
        username="zahra",
        status=SimpleNamespace(value="limited" if state == "expired" else "active"),
        used_traffic=int(used),
        data_limit=int(limit),
        expire=expire,
        on_hold_expire_duration=None,
    )
    apps = [
        SimpleNamespace(name=n, import_url=f"app://{n.lower()}/{{url}}", icon_url="", platform=p)
        for n, p in [("v2rayNG", "android"), ("Hiddify", "android"), ("V2Box", "ios"),
                     ("Streisand", "ios"), ("NekoBox", "android"), ("Happ", "ios")]
    ]
    links = [
        "vless://3063a663-51de-45ca-afa9-909256a34320@catwoman-panel.up.railway.app:443"
        "?encryption=none&security=tls&type=ws&headerType=none&path=%2Fws"
        "&host=catwoman-panel.up.railway.app&sni=catwoman-panel.up.railway.app"
        "#%F0%9D%97%96%F0%9D%97%94%F0%9D%97%A7%F0%9D%97%AA%F0%9D%97%A2%F0%9D%97%A0%F0%9D%97%94%F0%9D%97%A1%20%E2%80%A2%20zahra"
    ]
    return {
        "user": user,
        "links": links,
        "announce": "این پنل توسط Catwoman ساخته شده",
        "announce_url": "",
        "apps": apps,
    }


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sub.html")
    state = sys.argv[2] if len(sys.argv) > 2 else "normal"

    env = Environment(loader=FileSystemLoader([str(TEMPLATES)]), autoescape=False)
    env.filters["bytesformat"] = bytesformat
    env.filters["datetime"] = datetimeformat
    env.globals["now"] = lambda: datetime.now(timezone.utc)

    html = env.get_template("subscription/index.html").render(build(state))
    out.write_text(html, encoding="utf-8")
    print(f"  {out}  {len(html)} bytes  ({state})")


if __name__ == "__main__":
    main()
