# AI Coding Agents Container
# Lightweight container for running Claude Code and Codex CLI in isolation

FROM node:22-slim

LABEL maintainer="saipix"
LABEL description="Isolated AI coding agents execution environment"

# Install requirements from requirements.txt
COPY requirements.txt /tmp/requirements.txt

RUN apt-get update && \
    # Install gnupg first (needed for adding Microsoft repo)
    apt-get install -y --no-install-recommends gnupg && \
    # Parse requirements.txt: extract package names (before |), ignore comments
    grep -v '^#' /tmp/requirements.txt | grep -v '^$' | cut -d'|' -f1 | tr -d ' ' | \
    xargs apt-get install -y --no-install-recommends && \
    # Add Microsoft repo for mssql-tools (sqlcmd)
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
    echo "deb [arch=arm64,amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y mssql-tools18 unixodbc-dev && \
    ln -s /opt/mssql-tools18/bin/sqlcmd /usr/local/bin/sqlcmd && \
    ln -s /opt/mssql-tools18/bin/bcp /usr/local/bin/bcp && \
    # Lightweight editor (for crontab -e, etc.) + gosu for entrypoint user-switching
    apt-get install -y --no-install-recommends nano gosu && \
    # Debian ships fd as `fdfind`; agents look for `fd` too
    ln -sf /usr/bin/fdfind /usr/local/bin/fd && \
    # Cleanup
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/*

ENV EDITOR=nano

# Agent CLIs are RENDERED from the enabled harness modules' `agent_harnesses[].sandbox_package`
# declarations (see cli/_provisioning.render_sandbox_dockerfile) — exact-pinned so a stray
# upstream patch (e.g. Anthropic's claude-code 2.1.69 silently changing .mcp.json trust
# behavior) can't slip in unannounced. Customers bump via docker/.env without an agento
# release. Adding a harness needs NO edit here.
# {{ sandbox_package_args }}
# {{ sandbox_package_install }}

# Disable claude-code's built-in self-updater so the exact pin above can't
# be silently superseded at runtime.
ENV DISABLE_AUTOUPDATER=1

# Create non-root user matching host UID
# Handle case where UID already exists (e.g., node user is UID 1000)
ARG HOST_UID=501
ARG HOST_GID=20
RUN groupadd -g ${HOST_GID} agent 2>/dev/null || true && \
    if id -u ${HOST_UID} >/dev/null 2>&1; then \
        # UID exists - rename existing user and update home
        EXISTING_USER=$(getent passwd ${HOST_UID} | cut -d: -f1) && \
        usermod -l agent -d /home/agent -m "$EXISTING_USER" 2>/dev/null || true && \
        groupmod -n agent $(id -gn ${HOST_UID}) 2>/dev/null || true; \
    else \
        # UID doesn't exist - create new user
        useradd -m -s /bin/bash -u ${HOST_UID} -g ${HOST_GID} agent; \
    fi && \
    # Ensure home directory and .ssh exist with correct permissions
    mkdir -p /home/agent /home/agent/.ssh && \
    chown ${HOST_UID}:${HOST_GID} /home/agent /home/agent/.ssh && \
    chmod 700 /home/agent/.ssh

# Allow agent to write workspace files owned by node:node (GID 1000)
RUN usermod -aG node agent 2>/dev/null || true

# Let each installed agent CLI self-update: hand its package dir and binary to `agent`.
# Rendered from the harness declarations, so a new harness needs no edit here.
# {{ sandbox_package_chown }}

# Entrypoint: ensures config files exist and symlinks them to home
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Working directory (will be mounted)
WORKDIR /workspace

ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
