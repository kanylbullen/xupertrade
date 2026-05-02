# Caddy build with Cloudflare DNS-01 plugin for Let's Encrypt issuance.
# Required because the dashboard runs on an internal LAN domain — HTTP-01
# challenge can't reach us, so we use DNS-01 via the Cloudflare API.

FROM caddy:2-builder AS builder

RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare

FROM caddy:2-alpine

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
