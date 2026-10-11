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

# Drop to agent with every run uid's own group as a supplementary group, so the toolbox
# reads and writes a live run dir (owner = the run uid, group = its gid, mode 2770). The
# range is RUN_UID_BASE + RUN_UID_SLOTS per runner (framework/runner/uids.py; a test keeps
# the two in step). Not gosu: its initgroups would drop these groups. umask 002: the run uid
# can change what the toolbox writes there.
RUN_UID_BASE=20000
RUN_UID_SLOTS=256
count="${AGENTO_RUNNER_COUNT:-1}"
run_gids="$(seq "$RUN_UID_BASE" $((RUN_UID_BASE + count * RUN_UID_SLOTS - 1)) | paste -sd, -)"
HOME="$(getent passwd agent | cut -d: -f6)"
export HOME
umask 002
exec setpriv --reuid "$(id -u agent)" --regid "$(id -g agent)" --groups "$run_gids" "$@"
