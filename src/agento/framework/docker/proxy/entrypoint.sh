#!/bin/sh
# Writes the proxy->web shared secret once, builds the running Caddyfile, then starts
# Caddy. Only proxy and web mount the volume the secret lives on; that mount is the
# protection, not the mode.
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

# The share host is rendered into a site address, so it is checked, not quoted (SEC-5):
# a lowercase DNS name of two or more labels, no port, no wildcard. Empty = no shares.
dir="$(dirname "${AGENTO_PROXY_CADDYFILE:-/etc/agento-proxy/Caddyfile}")"
config="${AGENTO_PROXY_RUN_CONFIG:-/tmp/agento-Caddyfile}"
cat "${AGENTO_PROXY_CADDYFILE:-/etc/agento-proxy/Caddyfile}" > "$config"
valid_host() {
    # Letters spelled out: an a-z range follows the locale. Also refuses a newline.
    case "$1" in ''|*[!abcdefghijklmnopqrstuvwxyz0123456789.-]*|.*|*.|*..*) return 1 ;; esac
    case "$1" in *.*) ;; *) return 1 ;; esac
    [ "${#1}" -le 253 ] || return 1
    old_ifs="$IFS"; IFS=.; set -- $1; IFS="$old_ifs"  # no glob character is left
    for label in "$@"; do
        [ "${#label}" -le 63 ] || return 1
        case "$label" in -*|*-) return 1 ;; esac
    done
}
share="${AGENTO_SHARE_HOST:-}"
if [ -n "$share" ]; then
    if ! valid_host "$share"; then
        echo "proxy: AGENTO_SHARE_HOST is not a valid host name" >&2
        exit 1
    fi
    cat "$dir/share.caddy" >> "$config"
fi
exec caddy run --config "$config" --adapter caddyfile
