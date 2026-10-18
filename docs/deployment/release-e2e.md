# Release e2e on a self-hosted runner

Every published release — pre-release or full — runs the e2e suite (`pytest -m e2e`) with real
LLM calls and real credentials before it goes to PyPI. The `e2e` job in
`.github/workflows/release.yml` runs on a self-hosted runner with the label `agento-e2e`. When it
fails, the release is not published.

A draft release sends no GitHub event. To test a draft, go to **Actions → Release → Run workflow**
and give its tag or a branch. A manual run tests and builds. It never publishes.

## Why a self-hosted runner

- OAuth refresh tokens (Claude, Codex subscriptions) are **single-use**. Each refresh gives a new
  token and kills the old one. Only a stack that keeps its DB between runs can hold them. A
  GitHub-hosted runner starts empty each time.
- The credentials stay in the stack DB on the VPS. GitHub holds **no** LLM secret.

## One-time setup on the VPS

Use a dedicated VPS (or a dedicated user on it). Do **not** use the production host.

1. Create a user `gha` with Docker access. Install `uv` and Node 22.
2. Clone the repo to the stack dir (default `/srv/agento-e2e`; override with the repo variable
   `E2E_STACK_DIR`):
   ```bash
   git clone https://github.com/<org>/agento.git /srv/agento-e2e
   cd /srv/agento-e2e/docker && COMPOSE_BAKE=true docker compose -f docker-compose.dev.yml up -d --build
   cd .. && uv sync --group dev && uv run bin/agento setup:upgrade
   ```
3. Build an agent_view (the e2e tests take the first one from `workspace:build-status`).
4. Register the credentials **in this stack only**. Run `credential:register` over SSH once per
   harness: Claude, Codex, and Pi (an OpenRouter key; without it the Pi tests skip, and a skip
   fails the job). This is the one time you confirm the OAuth login by email. Add API keys in the same
   way.
   Log in again here; do **not copy a credential file** (for example
   `~/.claude/.credentials.json` from your laptop). A refresh token is single-use: the stack's
   first refresh rotates it, and every other copy of that file stops working. A separate login
   gives a separate token, so the stack and your laptop never collide.
5. Install the GitHub runner as a service under `gha`, with the label `agento-e2e`
   (Settings → Actions → Runners → New self-hosted runner).

## Repo settings (public repo)

A self-hosted runner in a public repo runs whatever a workflow tells it to run. Keep it locked:

- Only `release.yml` uses `runs-on: [self-hosted, agento-e2e]`. Never add it to a workflow with a
  `pull_request` trigger.
- Settings → Actions → General → **Require approval for all outside collaborators**.
- Settings → Environments → `e2e`: add yourself as a required reviewer. Limit deployments to tags.

## Keep the tokens alive

A token refreshes only when a job claims it close to its expiry (AG-65). If no job runs between
two releases, the token can expire. Run one cheap job from the VPS crontab every few hours. The
claim then rotates the token, and the stack stores the new one.

## A skip fails the job

The e2e tests skip themselves when the stack, an agent_view or a credential is missing. In the
`e2e` job, any `SKIPPED` line fails the run, because a skipped test does not prove the release.
