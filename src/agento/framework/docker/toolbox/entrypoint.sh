#!/bin/bash
set -e

# Toolbox has no agent secrets — no SSH/credentials to materialize at startup.
# We just drop privileges from root to the agent user so files written into
# the shared workspace/artifacts volume match the cron consumer's UID/GID.
# versioned_folders storage volume: created by Docker as root on first run.
# Non-recursive and failure-tolerant — an already-correct or absent mount must
# not stop the toolbox from booting.
if [ -d /srv/versioned-folders ]; then
    chown "$(id -u agent):$(id -g agent)" /srv/versioned-folders 2>/dev/null || true
fi

exec gosu agent "$@"
