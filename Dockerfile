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

# Xray core. The node runs it; the node binary does not ship one.
RUN set -eux; \
    curl -fsSL -o /tmp/xray.zip \
      "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip"; \
    unzip -q /tmp/xray.zip -d /usr/local/bin; \
    rm -f /tmp/xray.zip; \
    chmod +x /usr/local/bin/xray; \
    /usr/local/bin/xray version

# The node itself.
RUN set -eux; \
    curl -fsSL -o /tmp/node.zip \
      "https://github.com/PasarGuard/node/releases/download/${NODE_VERSION}/pasarguard-node-linux-amd64.zip"; \
    unzip -q /tmp/node.zip -d /usr/local/bin; \
    rm -f /tmp/node.zip; \
    mv /usr/local/bin/pasarguard-node-linux-amd64 /usr/local/bin/pasarguard-node; \
    chmod +x /usr/local/bin/pasarguard-node

COPY nginx.conf.template /opt/nginx.conf.template
COPY entrypoint.sh       /opt/entrypoint.sh
COPY bootstrap.py        /opt/bootstrap.py
COPY templates/          /opt/templates/
RUN chmod +x /opt/entrypoint.sh

ENV HTTP_PORT=8080 \
    DATA_DIR=/var/lib/pasarguard
EXPOSE 8080

ENTRYPOINT ["/opt/entrypoint.sh"]
