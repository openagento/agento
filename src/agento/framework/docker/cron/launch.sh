#!/bin/bash
# usage: launch.sh -- <argv...>
#
# The only way a process reaches uid `agent` in this container. `setpriv` PRESERVES the
# environment, so the environment is built here rather than inherited: root's own
# environment came from docker (or from `docker compose exec`) and is not the whitelist.
# The whitelist is /opt/cron-agent/env, written by the entrypoint, root-only. It holds
# MYSQL_* and the key: no agent runs in this container (docs/architecture/runner.md).
set -euo pipefail

# Start from an empty environment. The marker is a POSITIONAL argument, never a variable:
# a variable can be inherited, and `LAUNCH_CLEAN=1` arriving in the container environment
# would skip the scrub entirely.
if [ "${1:-}" != "--launch-clean-internal" ]; then
  exec env -i PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
       /bin/bash "$0" --launch-clean-internal "$@"
fi
shift

# NUL-delimited and imported WITHOUT evaluation: `.` would execute a `$(...)` inside a
# value as root, and would split a value containing a space.
if [ -r /opt/cron-agent/env ]; then
  while IFS= read -r -d '' entry; do
    name=${entry%%=*}
    [ "$name" = "$entry" ] && continue                            # no '=' at all
    case "$name" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;; esac   # skip, never abort
    export "$entry"
  done < /opt/cron-agent/env
fi

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
exec setpriv --reuid "$AGENT_UID" --regid "$AGENT_GID" --init-groups \
     env HOME=/home/agent USER=agent "$@"
