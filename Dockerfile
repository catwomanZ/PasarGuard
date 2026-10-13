# CATWOMAN - PasarGuard on Railway, one container.
#
# Built on the official panel image so the dashboard is already compiled in.
# Added on top: nginx, the node, and an Xray core. All three talk over
# loopback; only nginx is on a public port.
FROM pasarguard/panel:v5.4.1

ARG XRAY_VERSION=v26.9.30
ARG NODE_VERSION=v0.5.4
ARG TARGETARCH=amd64

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx openssl curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/nginx /var/lib/nginx/body /var/log/nginx

# Unpack in a scratch directory and move only the files we want.
#
# Never unzip straight into /usr/local/bin. Both release archives carry their
# own README.md, and the panel image already has one at that path, so unzip
# stops to ask "replace /usr/local/bin/README.md?" -- there is no one to answer
# during a build, it reads EOF, treats that as "no", and the build dies with
# exit code 1. Extracting elsewhere sidesteps the prompt entirely instead of
# papering over it with -o.

# Xray core. The node runs it; the node binary does not ship one.
RUN set -eux; \
    curl -fsSL -o /tmp/xray.zip \
      "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip"; \
    mkdir -p /tmp/xray; \
    unzip -q /tmp/xray.zip -d /tmp/xray; \
    install -m 0755 /tmp/xray/xray /usr/local/bin/xray; \
    install -m 0644 /tmp/xray/geoip.dat /usr/local/bin/geoip.dat; \
    install -m 0644 /tmp/xray/geosite.dat /usr/local/bin/geosite.dat; \
    rm -rf /tmp/xray /tmp/xray.zip; \
    /usr/local/bin/xray version

# The node itself.
RUN set -eux; \
    curl -fsSL -o /tmp/node.zip \
      "https://github.com/PasarGuard/node/releases/download/${NODE_VERSION}/pasarguard-node-linux-amd64.zip"; \
    mkdir -p /tmp/node; \
    unzip -q /tmp/node.zip -d /tmp/node; \
    install -m 0755 /tmp/node/pasarguard-node-linux-amd64 /usr/local/bin/pasarguard-node; \
    rm -rf /tmp/node /tmp/node.zip; \
    /usr/local/bin/pasarguard-node --help >/dev/null 2>&1 || true; \
    test -x /usr/local/bin/pasarguard-node

COPY nginx.conf.template /opt/nginx.conf.template
COPY entrypoint.sh       /opt/entrypoint.sh
COPY bootstrap.py        /opt/bootstrap.py
COPY selftest.py         /opt/selftest.py
COPY autohost.py         /opt/autohost.py
COPY templates/          /opt/templates/
RUN chmod +x /opt/entrypoint.sh

ENV HTTP_PORT=8080 \
    DATA_DIR=/var/lib/pasarguard
EXPOSE 8080

ENTRYPOINT ["/opt/entrypoint.sh"]
