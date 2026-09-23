#!/bin/bash
set -e

# Agent configs are materialized per-agent_view by `workspace:build`. Credentials are
# selected again per run and written into the artifacts HOME. The consumer sets
# HOME=<artifacts_dir>, so container startup does not prepare config symlinks. The SSH
# private key is never a file at all — it goes config -> env -> a per-run ssh-agent
# (framework/ssh_prelude.py); only the non-secret .ssh files are written per run.

# Set timezone for cron daemon (reads /etc/localtime, not TZ)
if [ -n "$TZ" ] && [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -sf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
fi

# Ensure log directory and existing log files are writable by agent.
# Recursive chown heals stale UIDs from prior builds (e.g. when HOST_UID
# changed between releases) — without it the consumer crash-loops on
# `PermissionError: '/app/logs/consumer.log'` because pre-existing log
# files are still owned by the previous agent UID.
mkdir -p /app/logs
chown -R agent /app/logs 2>/dev/null || true

# Heal artifacts dir from pre-fix deployments where toolbox wrote files as root.
# Idempotent and metadata-only — fast even on large dirs. The `|| true` guards
# against partially-inaccessible mounts; if chown can't fix it here, the
# diagnostic in prepare_artifacts_dir will surface a clear error.
if [ -d /workspace/artifacts ]; then
    chown -R agent:agent /workspace/artifacts 2>/dev/null || true
fi

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
# `CONFIG__AGENT_VIEW__IDENTITY__SSH_PRIVATE_KEY` is refused for a second reason too: the
# `su - agent` boundary below carries env through a line-oriented file, which cannot hold a
# multiline PEM and would put the key on disk if it could. Set it in the DB
# (`bin/agento config:set agent_view/identity/ssh_private_key ... --scope agent_view`).
for _v in AGENTO_SSH_PRIVATE_KEY AGENTO_SSH_TTL SSH_AUTH_SOCK SSH_AGENT_PID \
          GIT_SSH_COMMAND CONFIG__AGENT_VIEW__IDENTITY__SSH_PRIVATE_KEY; do
    if [ -n "${!_v:-}" ]; then
        echo "agento: $_v is set in the container environment - refusing to start." >&2
        echo "agento: the SSH identity is per-run config, never an ambient variable." >&2
        exit 78
    fi
done

# Persist Docker env vars for agent user (su - wipes the environment, cron has its own env)
ENV_FILE=/opt/cron-agent/env
env | grep -E '^(MYSQL_|TZ=|DISABLE_LLM=|DISABLE_AUTOUPDATER=|PROVIDER=|CONFIG__|AGENTO_|PYTHONPATH=)' > "$ENV_FILE"
chmod 644 "$ENV_FILE"

# Minimal crontab header — setup:upgrade populates the AGENTO:BEGIN/END block,
# Jira sync populates the JIRA-SYNC:BEGIN/END block.  Run 'bin/agento setup:upgrade'
# before starting containers to install cron jobs and apply migrations.
ENVLOAD="set -a; source $ENV_FILE; set +a"
cat <<CRONTAB | crontab -u agent -
SHELL=/bin/bash
PATH=${PATH}
PYTHONPATH=/opt/agento-src
HOME=/home/agent

CRONTAB

echo "Cron container started."

# Apply pending migrations and install crontab from module declarations
SETUP_DONE=/tmp/.setup-done
rm -f "$SETUP_DONE"
echo "Running setup:upgrade..."
su - agent -c "set -a; source $ENV_FILE; set +a; /opt/cron-agent/run.sh setup:upgrade --skip-onboarding" || {
    echo "setup:upgrade failed, exiting."
    exit 1
}
touch "$SETUP_DONE"

echo "Crontab after setup:upgrade:"
crontab -u agent -l

# Start consumer as background process (runs as agent user)
echo "Starting consumer process..."
su - agent -c "set -a; source $ENV_FILE; set +a; cd /workspace && /opt/cron-agent/run.sh consumer" &
CONSUMER_PID=$!

# Propagate signals to consumer
trap "kill $CONSUMER_PID 2>/dev/null; wait $CONSUMER_PID 2>/dev/null; exit 0" SIGTERM SIGINT

# Start cron daemon in background
cron -f &
CRON_PID=$!

# Wait for either process to exit (fail-fast)
wait -n $CONSUMER_PID $CRON_PID
