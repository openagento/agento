#!/bin/bash
# usage: launch.sh [--store] -- <argv...>
#
# The only way a process reaches uid `agent`. `setpriv` PRESERVES the environment (that is
# why it replaces `su - agent -c`, whose wipe is the sole reason a world-readable env file
# existed), so the environment must be built here rather than inherited: root's own
# environment came from docker and holds the credential store.
#
# The store never enters any environment, and never crosses an exec: with `--store` this
# script hands the command to root-owned `drop.py`, which reads the store, drops to uid
# `agent` IN-PROCESS and only then runs the CLI (see that file for why an exec would
# reopen a ptrace window).
set -euo pipefail

# Start from an empty environment. The marker is a POSITIONAL argument, never a variable:
# a variable can be inherited, and `LAUNCH_CLEAN=1` arriving in the container environment
# (the cron service loads ../secrets.env) would skip the scrub entirely.
if [ "${1:-}" != "--launch-clean-internal" ]; then
  exec env -i PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
       /bin/bash "$0" --launch-clean-internal "$@"
fi
shift

# Non-secret knobs only. NUL-delimited and imported WITHOUT evaluation: `.` would execute
# a `$(...)` inside a value as root, and would split a value containing a space.
if [ -r /opt/cron-agent/env.public ]; then
  while IFS= read -r -d '' entry; do
    name=${entry%%=*}
    [ "$name" = "$entry" ] && continue                            # no '=' at all
    case "$name" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;; esac   # skip, never abort
    export "$entry"
  done < /opt/cron-agent/env.public
fi

want_store=0
[ "${1:-}" = "--store" ] && { want_store=1; shift; }
[ "${1:-}" = "--" ] || { echo "launch.sh: bad usage" >&2; exit 64; }
shift
[ "$#" -gt 0 ] || { echo "launch.sh: no command" >&2; exit 64; }

# The agent account's primary group is whatever HOST_GID the image was built with (gid 20
# on a macOS host), and no group is NAMED `agent` — `setpriv --regid agent` fails outright.
# Resolve both ids numerically; --init-groups still reads the passwd entry for the
# supplementary groups.
AGENT_UID=$(id -u agent) || { echo "launch.sh: no agent user" >&2; exit 71; }
AGENT_GID=$(id -g agent) || { echo "launch.sh: no agent group" >&2; exit 71; }

cd /workspace
if [ "$want_store" = 1 ]; then
  # `drop.py` drops privilege itself, so this is the one branch that stays root — and the
  # only command it will run is the framework CLI. Anything else with the store is a
  # usage error, not something to launch.
  [ "$1" = /opt/cron-agent/run.sh ] || {
    echo "launch.sh: --store runs only /opt/cron-agent/run.sh" >&2; exit 64; }
  shift
  exec env HOME=/home/agent USER=agent PYTHONPATH=/opt/agento-src \
       /opt/cron-agent/.venv/bin/python /opt/cron-agent/drop.py "$@"
fi
exec setpriv --reuid "$AGENT_UID" --regid "$AGENT_GID" --init-groups \
     env HOME=/home/agent USER=agent "$@"
