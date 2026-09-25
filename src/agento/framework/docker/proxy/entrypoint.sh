#!/bin/sh
# Writes the proxy->web shared secret once, then starts Caddy. Only proxy and web
# mount the volume the file lives on; that mount is the protection, not the mode.
set -eu
secret="${AGENTO_PROXY_SECRET_FILE:-/run/agento/proxy-secret}"
if [ ! -s "$secret" ]; then
    tmp="$secret.tmp.$$"
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$tmp"
    chmod 0644 "$tmp"
    mv "$tmp" "$secret"
fi
AGENTO_PROXY_SECRET="$(cat "$secret")"
export AGENTO_PROXY_SECRET
exec caddy run --config "${AGENTO_PROXY_CADDYFILE:-/etc/agento-proxy/Caddyfile}" --adapter caddyfile
