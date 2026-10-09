# Runner

The runner is the one place where an agent CLI starts. It is a compose service on the
cron image, `runner-<i>`, with i = 1 … `AGENTO_RUNNER_COUNT` (default 1). It has no
database, no encryption key, no `env_file` and no `MYSQL_*` value. The worker (the consumer
in `cron`) keeps the database, the credential pool and the job state. It sends each run to
a runner over a Unix socket.

Code: `src/agento/framework/runner/` — `server.py`, `client.py`, `wire.py`, `listen.py`.

## Why

Before, the agent CLI ran in `cron`, as a child of the consumer. The consumer holds the
database password and the encryption key. A same-uid agent was kept away from them by a
stack of hardening (an env split, a store file, a privilege drop). The runner removed the
reason for that stack, so the stack is removed too: no agent runs in a container that holds
a secret ([zero-trust.md](zero-trust.md#what-the-runner-split-replaced)).

## Flow of one run

1. The worker selects and leases the credential, builds the run HOME on `/workspace`, and
   mints the toolbox capability, as before.
2. The consumer builds a `RemoteRunner` (not `create_runner`). It implements the same
   `Runner` protocol, so a module workflow sees no difference.
3. `RemoteRunner.execute(request)` connects to `runner-<n>.sock` (round robin) and sends one
   JSON line: the `HarnessRunContext` (with the credential), the `RunRequest`, `dry_run`,
   and `stream`.
4. The runner calls the harness's own `create_runner(...).execute()`. The
   `SubprocessRunner`, the output parser and the `stream_event_mapper` run there.
5. The runner sends NDJSON events back: `pid` (with the boot id), `session`, `fragment`
   (already mapped), `usage`, then `result` or `error`.
6. The worker saves the pid and the owner (`job.runner_ref`), the session id, submits the
   fragments (seq, redaction, sink) and writes the usage row. It finalizes the job from the
   `RunResult`, as before.

Other ops:

| Op | Use | Reply |
|----|-----|-------|
| `execute` | one agent run | events, then `result` or `error` |
| `run` | `subprocess.run` of one argv: codex login, the codex and pi model checks | `done` (rc, stdout, stderr) or `error` |
| `pty` | a CLI in a pseudo-terminal: the panel re-login | raw bytes both ways, then `\0<exit code>` |
| `alive` | is the run with this tag alive | `alive` or `dead`, with the boot id |
| `signal` | SIGTERM or SIGKILL to the run's process group | `sent`, with the boot id |

The client side: `runner.client.run`, `runner.client.pty`, `alive`, `signal`. A temporary
HOME for `run` or `pty` goes under `/workspace/.tmp` (`client.shared_tmp()`, mode 0700),
because the runner sees `/workspace` at the same path.

## Socket activation and the peer check

- `listen.py` runs as root, as a script. It runs no module code and dispatches no event
  (EVT-3). It makes `/run/agento-runner` `root:root 0755`, removes a stale socket, binds
  `runner-<i>.sock` with mode 0666, and starts the server as `agent` with the listener on
  fd 3 (`LISTEN_FDS=1`).
- The server never binds and never unlinks. It marks fd 3 close-on-exec, so no child holds
  the listener. `agent` cannot replace anything in a root 0755 directory, so only this
  container's server answers on that name.
- The server is the only child of the container's init (`init: true`), which reaps killed
  orphans. If the server dies, init exits, the PID namespace ends, the kernel kills every
  run, and compose restarts the container.
- Peer check: `SO_PEERCRED` gives the peer's pid as the runner's PID namespace sees it.
  The worker is in another container, so its pid is 0. An agent in the runner has a real
  pid and gets nothing. An agent in another runner is pid 0 too, so each socket has its own
  volume, `runner-<i>-sock`, mounted only by runner-<i> and by cron (at
  `/run/agento-runner/runner-<i>/`). Do not add `pid:` to a runner service: a shared PID namespace
  breaks this check. macOS has no `SO_PEERCRED`; there every peer is refused (tests inject
  the check).
- On SIGTERM (`compose stop`) the server exits at once. The runs die with the namespace.
  The worker gets "runner connection lost" with the last session id, and the durable retry
  resumes the session.

## The owner rule

`job.runner_ref = "<socket name>:<boot id>"` is saved with the pid. Only that socket can
say anything about the run:

| The owner socket … | `alive()` |
|--------------------|-----------|
| answers with the recorded boot id | its answer: `alive` or `dead` |
| answers with another boot id | `dead` (only a container start publishes that name) |
| does not answer (refused, missing, timeout) | `unknown` |

`unknown` blocks stale recovery and resume. No timeout turns `unknown` into `dead`. A
runner that never comes back leaves its jobs RUNNING, with a warning in the log. Start that
runner again: the new boot id makes the jobs `dead`, and they retry.

A job with a pid and no `runner_ref` is from before the runner. Its run was in the old cron
container, which the upgrade replaced, so it is `dead`. A job with no pid yet keeps the
`started_at` threshold.

**Lower `AGENTO_RUNNER_COUNT` only after a drain.** A removed runner name never answers,
so its jobs stay blocked. See [docker/README.md](../../docker/README.md).

## Limits (SEC-12, SCL-1)

| Bound | Default | Env |
|-------|---------|-----|
| children at one time | 200 | `AGENTO_RUNNER_MAX_PROCS` |
| extra connections for `alive`/`signal` | 32 | `AGENTO_RUNNER_MAX_CONTROL` |
| authorized connections per second (token bucket) | 500 | `AGENTO_RUNNER_ACCEPT_RATE` |
| refused peers | own bucket per pid, 1024 pids, 5 log lines a minute each | — |
| request line | 1 MiB | — |
| one event frame | 256 KiB; a larger fragment is cut and marked `truncated` | — |
| a long text field | sent as 16 Ki-character `output` chunks before its terminal event | — |
| text kept per field | the last 4 MiB, on both sides | — |

A refused peer does not use a token, so a flood of refused peers cannot starve the worker.

## Errors

The runner sends the error class name. The client raises the same class for the
credential errors (`AuthenticationError`, `TransientAuthError`, `UsageLimitError` with
`reset_at`, `ModelConfigError`), `subprocess.TimeoutExpired`, and every builtin the retry
policy names (`ValueError`, `KeyError`, `PermissionError`, `FileNotFoundError`), so the
retry decision does not change. Any other error is a `RuntimeError`. Each one carries
`session_id` and `agent_output`.

## What the runner logs

Tags, pids, exit codes and op names. Never a request line, a prompt, an env value or an
argv (SEC-6).

## The database

The runner sets `db.DISABLED`, so `get_connection` fails at once with "this process has no
database". A module observer that tries one in `bootstrap(db_conn=None)` fails fast. It
does not wait for a host it cannot reach. The runner re-runs `bootstrap()` when
`app/etc/modules.json` changes and no child runs.

## PID replacements

Every place that used a job pid with `os.kill` now asks the owner:

| Before | Now | Test |
|--------|-----|------|
| stale recovery: `Consumer._is_pid_alive(pid)` (`os.kill(pid, 0)`) | `runner_client.alive(job.runner_ref, "job:<id>")`; no ref and a pid → dead; no pid → `started_at` threshold | `TestRecoverStaleJobs` (`test_runner_socket_unknown_keeps_job_blocked`, `test_a_pid_from_before_the_runner_is_dead`) |
| resume gate: `_is_pid_alive(job.pid)` | `alive(...) != "dead"` — `unknown` counts as alive, so a session is never driven twice | `test_a_previous_run_that_may_still_go_on_is_never_resumed` |
| pause: `os.kill(pid, SIGTERM)`, then poll `os.kill(pid, 0)` | `runner_client.signal(ref, tag, SIGTERM)`, then poll `alive` | `TestPauseJob` |
| resume: clear `pid` | clear `pid` and `runner_ref` | `TestResumeJob` |
| timeout kill in `SubprocessRunner` | in the runner; at its end (exit or timeout) a run's whole process group is killed, also for `run`; `run` and `pty` are killed on EOF too | `test_no_descendant_outlives_its_run` |
| (new) the worker dies mid-run | the runner kills the process group on EOF, also when the child starts after the EOF | `test_a_closed_connection_kills_the_run`, `test_a_run_that_starts_after_the_worker_left_is_killed` |
| (new) the runner dies mid-run | `RuntimeError("runner connection lost")` with the session id → durable retry with resume | `test_a_lost_runner_is_a_retryable_error_with_the_session` |
| PTY login: `forkpty` in cron, `os.kill` on close | `pty` op; closing the socket kills the CLI | `tests/unit/agent_manager/test_pty_login.py` |

## Where a process may still start outside the runner

`tests/unit/framework/runner/test_spawn_guard.py` lists every file that may start a process,
with the reason. Outside the runner these are: host-side CLI commands (`docker`, `uv`), the
root crontab renderer and `run.sh` from the admin TUI. The attended operator login
(`credential:register` on a TTY) runs the harness's `start_web_login` in a runner, as the panel
re-login does, and reads the code from the operator's terminal (`auth.attended_login`).

## Tests

`tests/conftest.py` has `start_runner` and `runner_server`: an in-process server on a
temporary socket directory, with the peer check set to "outside". Integration tests start
one for every test (`tests/integration/conftest.py`), so a consumer run there goes through
the socket.
