#!/usr/bin/env bash
# End-to-end tunnel test.
#
# Stands up the same chain the container runs -- nginx with the real
# nginx.conf.template, the node, the Xray inbound the panel configured -- and
# puts a TLS terminator in front of it, because that is what Railway's edge
# does: TLS ends at the platform and the container only ever sees plain HTTP.
#
# Then a real Xray client imports the config the panel actually handed out and
# fetches a page through it. The client has ONLY the VLESS outbound -- no
# freedom fallback -- so a successful fetch can only have come through the
# tunnel.
#
#   ./test/tunnel-test.sh <public-host> <socks-port>
set -uo pipefail

HOSTNAME_TEST="${1:-panel.catwoman.test}"
SOCKS_PORT="${2:-10808}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK=/tmp/tunnel-test
NGINX=/usr/sbin/nginx

EDGE_PORT=8443          # stands in for Railway's edge (TLS)
HTTP_PORT=8080          # the container's own nginx (plain, behind the edge)
PANEL_PORT=8010
INBOUND_PORT=10085
TARGET_PORT=9111        # where the test page lives

rm -rf "$WORK"; mkdir -p "$WORK/edge/logs" "$WORK/run"
sudo mkdir -p /run/nginx /var/lib/nginx/body /var/log/nginx 2>/dev/null

pass() { printf '  \033[32m✅\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m❌\033[0m %s\n' "$1"; }
info() { printf '  %s\n' "$1"; }

echo
echo "── ۱) صفحه‌ی آزمایشی ──────────────────────────────────────────"
TOKEN="catwoman-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
mkdir -p "$WORK/target"
printf '%s\n' "$TOKEN" > "$WORK/target/token.txt"
(cd "$WORK/target" && nohup python3 -m http.server "$TARGET_PORT" --bind 127.0.0.1 >/dev/null 2>&1 &)
sleep 1
curl -fsS "http://127.0.0.1:$TARGET_PORT/token.txt" >/dev/null && pass "صفحه روی $TARGET_PORT بالاست"

echo
echo "── ۲) nginx با همان قالب واقعی پروژه ─────────────────────────"
sed -e "s|@@HTTP_PORT@@|$HTTP_PORT|g" \
    -e "s|@@PANEL_PORT@@|$PANEL_PORT|g" \
    -e "s|@@INBOUND_PORT@@|$INBOUND_PORT|g" \
    -e "s|@@TUNNEL_PATH@@|/ws|g" \
    "$ROOT/nginx.conf.template" > "$WORK/nginx.conf"
$NGINX -t -c "$WORK/nginx.conf" >/dev/null 2>&1 \
  && pass "کانفیگ معتبر است" || { fail "کانفیگ نامعتبر"; $NGINX -t -c "$WORK/nginx.conf"; exit 1; }
$NGINX -c "$WORK/nginx.conf" 2>/dev/null
sleep 1
curl -fsS -o /dev/null "http://127.0.0.1:$HTTP_PORT/healthz" \
  && pass "nginx روی $HTTP_PORT بالاست و /healthz را به پنل می‌رساند" \
  || fail "nginx بالا نیامد"

echo
echo "── ۳) شبیه‌ساز لبه‌ی Railway (TLS روی $EDGE_PORT) ─────────────"
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout "$WORK/edge.key" -out "$WORK/edge.crt" \
  -subj "/CN=$HOSTNAME_TEST" \
  -addext "subjectAltName=DNS:$HOSTNAME_TEST" 2>/dev/null
cat > "$WORK/edge.conf" <<EOF
worker_processes 1;
error_log $WORK/edge/logs/error.log warn;
pid $WORK/run/edge.pid;
events { worker_connections 256; }
http {
  access_log $WORK/edge/logs/access.log;
  map \$http_upgrade \$up { default upgrade; '' close; }
  server {
    listen $EDGE_PORT ssl;
    server_name $HOSTNAME_TEST;
    ssl_certificate     $WORK/edge.crt;
    ssl_certificate_key $WORK/edge.key;
    location / {
      proxy_pass http://127.0.0.1:$HTTP_PORT;
      proxy_http_version 1.1;
      proxy_set_header Upgrade \$http_upgrade;
      proxy_set_header Connection \$up;
      proxy_set_header Host \$host;
      proxy_read_timeout 300s;
    }
  }
}
EOF
$NGINX -c "$WORK/edge.conf" 2>/dev/null
sleep 1
curl -fsS -o /dev/null --resolve "$HOSTNAME_TEST:$EDGE_PORT:127.0.0.1" \
  "https://$HOSTNAME_TEST:$EDGE_PORT/healthz" \
  && pass "لبه با SNI=$HOSTNAME_TEST جواب می‌دهد" \
  || fail "لبه جواب نداد"

echo
echo "── ۴) استخراج کانفیگ از لینکی که پنل داد ────────────────────"
TOK=$(cat /tmp/tok.txt); SUB=$(cat /tmp/suburl.txt)
curl -s -A "v2rayNG/1.8" "http://127.0.0.1:$PANEL_PORT${SUB}/links_base64" | base64 -d > "$WORK/link.txt"
LINK=$(head -1 "$WORK/link.txt")
[ -n "$LINK" ] && pass "لینک گرفته شد" || { fail "لینک خالی"; exit 1; }

python3 - "$LINK" "$WORK/client.json" "$SOCKS_PORT" "$EDGE_PORT" "$HOSTNAME_TEST" <<'PY'
import json, sys, urllib.parse
link, out, socks, edge_port, hostname = sys.argv[1:6]
u = urllib.parse.urlparse(link)
q = urllib.parse.parse_qs(u.query)
one = lambda k, d='': (q.get(k) or [d])[0]
cfg = {
  "log": {"loglevel": "warning"},
  "inbounds": [{
    "tag": "socks",
    "listen": "127.0.0.1",
    "port": int(socks),
    "protocol": "socks",
    "settings": {"auth": "noauth", "udp": False, "address": "127.0.0.1"},
  }],
  # Only this outbound. No freedom, so nothing can escape the tunnel by accident.
  "outbounds": [{
    "tag": "proxy",
    "protocol": "vless",
    "settings": {
      "vnext": [{
        "address": "127.0.0.1",
        "port": int(edge_port),
        "users": [{"id": u.username, "encryption": "none", "level": 0}],
      }]
    },
    "streamSettings": {
      "network": one("type"),
      "security": one("security"),
      "tlsSettings": {
        "serverName": one("sni"),
        "allowInsecure": True,
        "fingerprint": one("fp", "chrome"),
      },
      "wsSettings": {"path": one("path"), "headers": {"Host": one("host")}},
    },
  }],
}
json.dump(cfg, open(out, "w"), indent=2)
print("  کانفیگ کلاینت ساخته شد از خود لینک:")
print("     sni =", one("sni"), " host =", one("host"), " path =", one("path"))
PY

echo
echo "── ۵) تست اتصال واقعی ───────────────────────────────────────"
nohup /usr/local/bin/xray run -c "$WORK/client.json" > "$WORK/client.log" 2>&1 &
CLIENT_PID=$!
sleep 2
if ! kill -0 $CLIENT_PID 2>/dev/null; then fail "کلاینت اجرا نشد"; tail -5 "$WORK/client.log"; exit 1; fi
pass "کلاینت Xray روی socks5 127.0.0.1:$SOCKS_PORT بالاست"

GOT=$(curl -s --max-time 20 --socks5-hostname "127.0.0.1:$SOCKS_PORT" \
      "http://127.0.0.1:$TARGET_PORT/token.txt" 2>/dev/null | tr -d '\n')
if [ "$GOT" = "$TOKEN" ]; then
  pass "داده از تونل رد شد — محتوا دقیقاً درست است"
  RESULT=0
else
  fail "تونل کار نکرد (گرفتم: '${GOT:0:40}')"
  RESULT=1
fi

echo
echo "── ۶) پنل مصرف را شمرد؟ ─────────────────────────────────────"
sleep 2
curl -s "http://127.0.0.1:$PANEL_PORT/api/user/zahra" -H "Authorization: Bearer $TOK" \
  | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin); n=d.get('used_traffic',0)
    print('  مصرف ثبت‌شده در پنل: %d بایت  %s' % (n, '✅ شمرده شد' if n>0 else '⚠ هنوز صفر (همگام‌سازی تاخیر دارد)'))
except Exception as e: print('  ?', e)"

echo
echo "── نتیجه ────────────────────────────────────────────────────"
[ $RESULT -eq 0 ] && printf '  \033[32mتونل کامل کار می‌کند.\033[0m\n' \
                  || printf '  \033[31mتونل کار نکرد.\033[0m\n'
echo "  لاگ کلاینت: $WORK/client.log"
echo "  لاگ لبه:    $WORK/edge/logs/access.log"
exit $RESULT
