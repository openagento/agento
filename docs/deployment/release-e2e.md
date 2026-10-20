# Release e2e on a self-hosted runner

Every published release — pre-release or full — runs the e2e suite (`pytest -m e2e`) with real
LLM calls and real credentials before it goes to PyPI. The `e2e` job in
`.github/workflows/release.yml` runs on a self-hosted runner with the label `agento-e2e`. When it
fails, the release is not published.

A draft release sends no GitHub event. To test a draft, go to **Actions → Release → Run workflow**
and give its tag or a branch. A manual run tests, builds and runs e2e. It never publishes.

## Why a self-hosted runner

- OAuth refresh tokens (Claude, Codex subscriptions) are **single-use**. Each refresh gives a new
  token and kills the old one. Only a stack that keeps its DB between runs can hold them. A
  GitHub-hosted runner starts empty each time.
- The credentials stay in the stack DB. GitHub holds **no** LLM secret.

## The stack under test

The job uses an existing **PyPI-layout deployment**, not a checkout: by default
`/home/mklauza/agento-mklauza` on cyber1 (override with the repo variable `E2E_STACK_DIR`). It
has `.venv/` with agento-core installed, `docker/docker-compose.yml` + override, `app/etc/`,
`secrets.env` and `workspace/`. It has no `src/`, `tests/` or `bin/agento`; the CLI is
`.venv/bin/agento`.

## Flow

1. `build` makes the wheel and uploads it as the `dist` artifact.
2. `e2e` checks out the same ref (for the tests) and downloads `dist`.
3. `"$STACK/bin/upgrade.sh" <wheel>` installs **that exact wheel** into the stack. It backs up the
   config, installs the wheel from `~/agento-candidates/<version>/`, runs
   `agento upgrade --version <v> --no-restart` (the old containers keep serving while the images
   build), recreates every container except mysql (migrations run on start), and checks the
   images, toolbox health and venv version. On a failed build it restores the previous version.
   It exits non-zero on any failure. A wheel equal to the installed version is allowed, so the
   same candidate can be re-tested.
4. The job waits (up to 120 s) until `.venv/bin/agento workspace:build-status` answers.
   `upgrade.sh` returns when cron is started, not ready, and the tests discover their
   agent_view at collection time — without the wait they skip.
5. `uv sync --group dev`, then, with `AGENTO_E2E=1 AGENTO_E2E_PROJECT_ROOT=$STACK`:
   `pytest -m "e2e and not rotates_credentials and not serial"` in parallel (one worker per CPU), then
   `pytest -m "e2e and rotates_credentials" -n 0` on its own. The tests read modules from the
   installed package and call the stack's `.venv/bin/agento`.
   The `serial` tests do not run here: the proxy smoke needs a dev checkout's stack, and the
   orchestration benchmark runs in the `test` job against its MySQL service.
6. `publish` runs only when `e2e` is green.

A failed e2e leaves the stack on the candidate. This is intended: inspect it there.

## Requirements on the stack host

- The runner runs as the stack's owner, in the `docker` group, with the label `agento-e2e`.
- `bin/upgrade.sh` is present in the stack. It lives on the host, not in this repo.
- `requires-python` in the stack's `pyproject.toml` is capped at `<3.15`. The job also sets
  `UV_PYTHON=3.12`: unpinned, uv picks the newest managed CPython, and `regex` has no wheel for
  it, so `uv sync` tries to compile it.
- Do not delete `~/agento-candidates/`. The stack's `uv.lock` points at it.

## Credentials

A skipped test fails the job, so every harness needs a credential:

| Harness | Needs |
|---|---|
| Claude | a Claude credential in the stack (OAuth preferred) |
| Codex | a Codex credential in the stack (OAuth preferred) |
| Pi | an OpenRouter key in the stack, with credit (the test model is paid; `:free` ids are rate-limited) |

Every agent CLI runs in the **sandbox**, at the version the release pins. Nothing is
installed on the host, and no key goes into `secrets.env` for the tests.

Register each credential **in the stack** with `credential:register`. Do **not copy a
credential file** (for example `~/.claude/.credentials.json` from your laptop). A refresh token
is single-use: the stack's first refresh rotates it, and every other copy of that file stops
working. A separate login gives a separate token, so the stack and your laptop never collide.

## The first agent_view

The suite runs as the **first agent_view in `workspace:build-status`**, and changes neither its
config nor the credential pool. Every run pins its harness, model and credential: the
matrix with `agento e2e --harness H --model M --credential ID` (a pinned job,
[job-runtime.md](../cli/job-runtime.md)), the Claude and Codex contract suites with the same
options on `agento run` ([run.md](../cli/run.md)). So the tests run in parallel, and the
stack's real cron jobs keep running on what they are configured for while the suite runs.

The one exception is `test_refresh_real_cli`: it moves a real token's expiry and rotates its
single-use refresh token. A run using that token at the same moment could rotate it too, so
it is marked `rotates_credentials` and runs alone, after the rest.

## Keep the tokens alive

A token refreshes only when a job claims it close to its expiry (AG-65). If no job runs for a
long time, the refresh token can expire. Run one cheap job from the host crontab every few
hours. The claim then rotates the token, and the stack stores the new one.

## Security (public repo)

The runner is in the `docker` group, so it has root-equivalent access to the host. Keep it
locked:

- Only `release.yml` uses `runs-on: [self-hosted, agento-e2e]`. Never add the label to `ci.yml`
  or to any workflow with a `pull_request` trigger.
- Fork-PR approval stays **Require approval for all outside collaborators**
  (`all_external_contributors`).
- Settings → Environments → `e2e`: a required reviewer. Limit deployments to tags.
- No LLM credential goes into GitHub secrets.

## A skip fails the job

The e2e tests skip themselves when the stack, an agent_view or a credential is missing. In the
`e2e` job, any `SKIPPED` line fails the run, because a skipped test does not prove the release.
