ARG SANDBOX_IMAGE=agento-sandbox:latest
FROM ${SANDBOX_IMAGE}

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends cron python3 python3-venv procps logrotate && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv

ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python

# Subdirectory of the build context that contains pyproject.toml + uv.lock.
# Default '.' = client-side context (`.agento/docker/cron/` materialized via
# `agento install`). Dev compose builds with context=repo-root and keeps '.'
# (root repo has pyproject.toml + uv.lock at top level).
ARG PYPROJECT_DIR=.
# Subdirectory of the build context that contains the cron service scripts.
# Default '.' = client-side context. Dev compose overrides to
# 'src/agento/framework/docker/cron'.
ARG SCRIPTS_DIR=.

# Container venv with native deps (cryptography, pymysql, etc.) only.
# --no-install-project: agento itself is bind-mounted from host site-packages
# at runtime via PYTHONPATH; we only need its dependencies pre-installed here.
# _local_dist/: local wheels (when CLI was installed from a local build).
# Always present (may be empty); see localize_lockfile_for_container().
COPY ${PYPROJECT_DIR}/pyproject.toml ${PYPROJECT_DIR}/uv.lock /opt/cron-agent/
COPY ${PYPROJECT_DIR}/_local_dist/ /opt/cron-agent/_local_dist/
RUN cd /opt/cron-agent && \
    uv venv .venv && \
    uv sync --frozen --no-install-project --no-dev

ENV PATH=/opt/cron-agent/.venv/bin:$PATH

COPY ${SCRIPTS_DIR}/run.sh /opt/cron-agent/run.sh
COPY ${SCRIPTS_DIR}/logrotate-agent.conf /opt/cron-agent/logrotate-agent.conf
COPY ${SCRIPTS_DIR}/launch.sh /opt/cron-agent/launch.sh
COPY ${SCRIPTS_DIR}/install-crontab.py /opt/cron-agent/install-crontab.py
COPY ${SCRIPTS_DIR}/split-env.py /opt/cron-agent/split-env.py
COPY ${SCRIPTS_DIR}/drop.py /opt/cron-agent/drop.py
COPY ${SCRIPTS_DIR}/entrypoint.sh /entrypoint.sh

# /opt/cron-agent is root-owned: write permission on the DIRECTORY would let `agent`
# unlink or replace a root-owned script inside it whatever that file's own mode says.
# `agent` reads and executes run.sh and the venv, and writes nothing. The three
# root-only programs are 0700 — the launcher, the renderer, the env splitter and the
# privilege-dropping store reader.
RUN chown -R root:root /opt/cron-agent && \
    chmod -R go-w /opt/cron-agent && \
    chmod +x /opt/cron-agent/run.sh /entrypoint.sh && \
    chmod 0700 /opt/cron-agent/launch.sh /opt/cron-agent/install-crontab.py \
               /opt/cron-agent/split-env.py /opt/cron-agent/drop.py

ENTRYPOINT ["/entrypoint.sh"]
