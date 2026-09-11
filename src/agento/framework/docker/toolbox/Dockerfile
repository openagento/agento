FROM node:22-slim

LABEL description="MCP toolbox - thin runtime, source mounted at runtime"

WORKDIR /app

# Subdirectory of the build context that contains package.json + package-lock.json.
# Default '.' = client-side context (`.agento/docker/toolbox/` materialized via
# `agento install`). Dev compose overrides to 'src/agento/toolbox'.
ARG PACKAGE_DIR=.

# Subdirectory of the build context that contains entrypoint.sh + the Dockerfile.
# Default '.' = client-side context. Dev compose builds from repo root and
# overrides to 'src/agento/framework/docker/toolbox'.
ARG DOCKERFILE_DIR=.

COPY ${PACKAGE_DIR}/package.json ${PACKAGE_DIR}/package-lock.json ./
RUN npm ci --omit=dev

# Pin Playwright browser cache to a known location so we can chown it
# deterministically below (default would be /root/.cache/ms-playwright).
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# Install Chromium + system dependencies for Playwright MCP browser tools.
# Note: Google Chrome has no official Linux ARM64 build; Chromium supports ARM64.
RUN npx playwright install --with-deps chromium

# poppler-utils for PDF-to-text conversion (pdftotext CLI).
# gosu for entrypoint user-switching (mirrors sandbox/cron pattern).
# git backs the versioned_folders storage engine (bare repos + linked worktrees
# under /srv/versioned-folders). Toolbox only — the store is never mounted into the
# agent container, and no generic git operation on it is exposed to the agent.
RUN apt-get update && \
    apt-get install -y --no-install-recommends poppler-utils gosu git && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# OpenSearch uses self-signed certs on internal network
ENV NODE_TLS_REJECT_UNAUTHORIZED=0
ENV NODE_PATH=/app/node_modules

# Server.js is loaded from /opt/agento-toolbox-src/ (bind-mounted at runtime).
# Node ESM walks up from the importing file looking for node_modules/ and ignores
# NODE_PATH, so place a symlink one level above the bind mount where Node will
# find it. We can't put it inside /opt/agento-toolbox-src/ — the bind mount
# would shadow it.
RUN ln -s /app/node_modules /opt/node_modules

# Create non-root user matching host UID/GID. Mirrors sandbox/Dockerfile so the
# toolbox writes files into shared workspace/artifacts with the same ownership
# the cron consumer (also `agent`) uses — otherwise rmtree on retry fails.
ARG HOST_UID=1000
ARG HOST_GID=1000
RUN groupadd -g ${HOST_GID} agent 2>/dev/null || true && \
    if id -u ${HOST_UID} >/dev/null 2>&1; then \
        EXISTING_USER=$(getent passwd ${HOST_UID} | cut -d: -f1) && \
        usermod -l agent -d /home/agent -m "$EXISTING_USER" 2>/dev/null || true && \
        groupmod -n agent $(id -gn ${HOST_UID}) 2>/dev/null || true; \
    else \
        useradd -m -s /bin/bash -u ${HOST_UID} -g ${HOST_GID} agent; \
    fi && \
    mkdir -p /home/agent && \
    chown -R ${HOST_UID}:${HOST_GID} /app /opt/playwright /home/agent && \
    chown -h ${HOST_UID}:${HOST_GID} /opt/node_modules

COPY ${DOCKERFILE_DIR}/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 3001

# Liveness check: HTTP 200 on /health = toolbox process responds. Does NOT check
# Playwright (intentionally — a dead Playwright leaves the container healthy so
# other adapters keep serving). curl is not in node:22-slim; node has native fetch.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD node --eval "fetch('http://127.0.0.1:3001/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"

# Source code (server.js, adapters/, etc.) is bind-mounted to /opt/agento-toolbox-src/.
# Modules from agento.modules + app/code are mounted to /app/modules/{core,user}.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["node", "/opt/agento-toolbox-src/server.js"]
