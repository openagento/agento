#!/bin/bash
set -e

# Agent configs come from per-agent_view workspace builds. Credentials are selected again
# per run and written into the artifacts HOME. The consumer and `agento run` set
# HOME=<artifacts_dir>, so there is nothing to prepare here. The SSH private key is never
# a file at all — it goes config -> env -> a per-run ssh-agent (framework/ssh_prelude.py).

# No SSH identity may survive in a passwd home. `ssh` resolves ~/.ssh through getpwuid(),
# not $HOME, so /home/agent/.ssh and /root/.ssh are DEFAULT identity directories for every
# run: one run could plant an id_rsa there and a later run granted no key would silently
# use it. Runs as root, so this is the only place /root/.ssh can be dealt with — the spawn
# prelude runs as `agent` and cannot. FAIL CLOSED: a path we cannot clear is an active
# isolation break, not a warning.
for _d in /home/agent/.ssh /root/.ssh; do
    rm -rf "$_d" 2>/dev/null || true
    if [ -e "$_d" ] || [ -L "$_d" ]; then
        echo "agento: $_d exists and could not be removed - refusing to start" >&2
        exit 78
    fi
done

# A run's SSH identity is resolved PER RUN from that agent_view's config and delivered to
# one process. None of these names may arrive from the container environment: an ambient
# value would be inherited by every agent_view's runs, including views that were granted
# no key at all, which is exactly the cross-view use of one identity this release removes.
# `CONFIG__AGENT_VIEW__IDENTITY__SSH_PRIVATE_KEY` is refused here for the same reason as the
# others, and stays refused for consistency with the cron entrypoint, whose `su - agent`
# boundary carries env through a line-oriented file that cannot hold a multiline PEM (this
# entrypoint `exec`s gosu and writes no such file). Set it in the DB instead
# (`cat id_rsa | bin/agento config:set agent_view/identity/ssh_private_key --scope agent_view`).
for _v in AGENTO_SSH_PRIVATE_KEY AGENTO_SSH_TTL SSH_AUTH_SOCK SSH_AGENT_PID \
          GIT_SSH_COMMAND CONFIG__AGENT_VIEW__IDENTITY__SSH_PRIVATE_KEY; do
    if [ -n "${!_v:-}" ]; then
        echo "agento: $_v is set in the container environment - refusing to start." >&2
        echo "agento: the SSH identity is per-run config, never an ambient variable." >&2
        exit 78
    fi
done

exec gosu agent "$@"
