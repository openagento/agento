#!/bin/bash
set -e

# Toolbox has no agent secrets — no SSH/credentials to materialize at startup.
# We just drop privileges from root to the agent user so files written into
# the shared workspace/artifacts volume match the cron consumer's UID/GID.
# versioned_artifacts storage volume: created by Docker as root on first run.
# Non-recursive and failure-tolerant — an already-correct or absent mount must
# not stop the toolbox from booting.
if [ -d /srv/versioned-artifacts ]; then
    mkdir -p /srv/versioned-artifacts/store /srv/versioned-artifacts/published 2>/dev/null || true
    for d in /srv/versioned-artifacts /srv/versioned-artifacts/store /srv/versioned-artifacts/published; do
        chown "$(id -u agent):$(id -g agent)" "$d" 2>/dev/null || true
    done
fi

exec gosu agent "$@"
