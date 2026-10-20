# Runner

The runner is the one place where an agent CLI starts. It is a compose service on the
cron image, `runner-<i>`, with i = 1 … `AGENTO_RUNNER_COUNT` (default 1). It has no
database, no encryption key, no `env_file` and no `MYSQL_*` value. The worker (the consumer
in `cron`) keeps the database, the credential pool and the job state. It sends each run to
a runner over a Unix socket.

Code: `src/agento/framework/runner/` — `server.py`, `worker.py`, `uids.py`, `client.py`,
`wire.py`, `listen.py`.

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
4. The runner takes a uid for the run, gives it the run HOME, and starts a run worker
   (`worker.py`) as that uid ([Per-run uid](#per-run-uid)). The worker calls the harness's
   own `create_runner(...).execute()`. The `SubprocessRunner`, the output parser and the
   `stream_event_mapper` run there, not in the root server.
5. The runner relays the worker's NDJSON events: `pid` (with the boot id), `session`,
   `fragment` (already mapped), `usage`, then `result` or `error`. It holds the last event
   until its cleanup is done (every process of the uid stopped, the HOME given back to
   `agent`), so the worker reads the HOME (credential capture) only when it can.
6. The worker saves the pid and the owner (`job.runner_ref`), the session id, submits the
   fragments (seq, redaction, sink) and writes the usage row. It finalizes the job from the
   `RunResult`, as before.

Other ops:

| Op | Use | Reply |
|----|-----|-------|
| `execute` | one agent run | events, then `result` or `error` |
| `run` | `subprocess.run` of one argv: codex login, the codex and pi model checks, the out-of-band credential refresh (AG-65) | `done` (rc, stdout, stderr) or `error` |
| `pty` | a CLI in a pseudo-terminal: the panel re-login | raw bytes both ways, then `\0<exit code>` |
| `alive` | is the run with this tag alive | `alive` or `dead`, with the boot id |
| `signal` | SIGTERM or SIGKILL to every process of the run's uid | `sent`, with the boot id |

The client side: `runner.client.run`, `runner.client.pty`, `alive`, `signal`. A temporary
HOME for `run` or `pty` goes under `/workspace/.tmp` (`client.shared_tmp()`), because the
runner sees `/workspace` at the same path. `/workspace/.tmp` and the panel login directory
under it are `0711` (`client.traversable`): a run uid passes through them to its own HOME,
and lists nothing. `cron` and `web` (for a `local` config tester) both call the client.

## Socket activation and the peer check

- `listen.py` runs as root, as a script. It runs no module code and dispatches no event
  (EVT-3). It makes `/run/agento-runner` `root:root 0755`, removes a stale socket, binds
  `runner-<i>.sock` with mode 0666, and starts the server (still root) with the listener on
  fd 3 (`LISTEN_FDS=1`).
- The server never binds and never unlinks. It marks fd 3 close-on-exec, so no child holds
  the listener. No run uid can replace anything in a root 0755 directory, so only this
  container's server answers on that name.
- The server is the only child of the container's init (`init: true`), which reaps killed
  orphans. If the server dies, init exits, the PID namespace ends, the kernel kills every
  run, and compose restarts the container.
- Peer check: `SO_PEERCRED` gives the peer's pid as the runner's PID namespace sees it.
  The worker is in another container, so its pid is 0. An agent in the runner has a real
  pid and gets nothing. An agent in another runner is pid 0 too, so each socket has its own
  volume, `runner-<i>-sock`, mounted only by runner-<i> and, read-only, by `cron` and `web`
  (the backend, DECISIONS.md D-BACKEND-1) at `/run/agento-runner/runner-<i>/`. Do not add `pid:` to a runner service: a shared PID namespace
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
| children at one time | 200; at most 256 (one uid each), or the server does not start | `AGENTO_RUNNER_MAX_PROCS` |
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

## Crash handling

A run that ends with a plain crash gets one more try in the same attempt. The consumer
(`Consumer._execute_with_one_retry`) resumes the session once, with an empty prompt, the
same credential, lease and HOME, when all of these are true:

- the error class is exactly `RuntimeError` (a credential error or a timeout keeps its own path),
- the error carries a `session_id`,
- the harness declares `capabilities.resume`,
- a new read of the job shows it is still `RUNNING` (a paused job is never resumed),
- the run's owner answers `dead` for its tag; `alive` or `unknown` (a lost connection) leaves
  the job to the durable retry, which applies the same rule (DECISIONS.md 2026-10-09).

The consumer writes one log line, `process_retry` (job id, attempt), and no event: a retry
inside one attempt is not a state change (EVT-1). An error from the second call goes to the
same handlers as an error from the first call: an auth error poisons the credential, a usage
limit throttles it, and the text is redacted. A second crash spends the attempt and takes the
ordinary retry. No job makes a third call in one attempt
(`test_every_crash_is_resumed_once_at_scale`, at 100 parallel jobs).

## What the runner logs

Tags, pids, exit codes and op names, and a warning when a uid is kept
([Per-run uid](#per-run-uid)). Never a request line, a prompt, an env value or an argv
(SEC-6).

## The database

The root server imports no harness and no module code. Each run worker loads only the
harness registry (`harness_reloader`, `framework/harness/loader.py`: no observer, no event,
EVT-3), so a module enabled after start is used by the next run with no restart. The worker
sets `db.DISABLED`, so `get_connection` fails at once with "this process has no database";
a harness that tries one fails fast and does not wait for a host it cannot reach.

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
| (new) the runner dies mid-run | `RuntimeError("runner connection lost")` with the session id → one resume in the same attempt only when the owner answers `dead` (a restarted runner has a new boot id); `unknown` skips it ([Crash handling](#crash-handling)); then the durable retry with resume | `test_a_lost_runner_is_a_retryable_error_with_the_session` |
| PTY login: `forkpty` in cron, `os.kill` on close | `pty` op; closing the socket kills the CLI | `tests/unit/agent_manager/test_pty_login.py` |

## Per-run uid

Every child op (`execute`, `run`, `pty`) runs as its own uid, so one run cannot read or
signal another run in the same runner: not its HOME, not its toolbox capability, not its
credential.

- **The server is root with seven capabilities.** Compose gives each `runner-<i>`
  `cap_drop: [ALL]` and `cap_add: [SETUID, SETGID, CHOWN, FOWNER, FSETID, DAC_READ_SEARCH,
  KILL]`, with `no-new-privileges`. It has no `DAC_OVERRIDE`: it never creates, writes or
  removes a file in a run dir. It only walks the dir and changes owners and modes.
- **Uid ranges.** Runner i owns the uids `20000 + (i-1)*256` to `+255` (`uids.RUN_UID_BASE`,
  `RUN_UID_SLOTS`). The ranges never overlap, so a uid is never in two runners at once. A uid
  has a primary group of the same number and no other group, except the gids in
  `AGENTO_RUN_GROUPS` (D-RUN-GROUPS-1). No `/etc/passwd` entry is needed.
- **The run dir.** For `execute` it is the context's `home_dir` (a job with no agent_view gets a temp
  HOME under `/workspace/.tmp` from the consumer); for
  `run` the `HOME` it is given (else `cwd`); for `pty` its `home`. Its path must be a
  directory strictly under `/workspace/artifacts/` or `/workspace/.tmp/` with no symlink
  on it, the two roots included, or the op is refused. The server opens it component by
  component from `/` with `O_NOFOLLOW` (it never resolves a real path), and walks it on
  descriptors only (`os.fwalk`, no link followed), so a path an `agent` process swaps
  after the check never moves the walk out of the dir. Before the spawn, the server gives
  the dir and everything in it to `uid:uid`, each entry through its own descriptor, and sets every
  directory to `2770` (setgid: a new entry keeps the run's gid).
  A file with another hard link keeps its owner on the way in: that inode can also be outside
  the dir. On the way back it is changed when that run's own uid or gid holds it, so the uid keeps
  nothing for its next run; an inode of another run keeps its owner.
  A symlink and a socket keep their owner too.
  File modes do not change: a CLI checks some of them (ssh refuses a group-writable
  `~/.ssh/config`).
- **The toolbox** gets every run gid of every runner as a supplementary group
  (`AGENTO_RUNNER_COUNT` in its `environment:`; its entrypoint uses `setpriv --groups`, not
  gosu, which would drop them) and runs with `umask 002`. So it writes a Jira attachment or
  a versioned-artifact desk into a live run dir, and the run can change what it wrote
  (`desk-io.js` uses `0o660` files and `0o770` dirs). Another run is "other" and gets nothing.
- **The cleanup, in this order**, also after an error: stop every process of the uid
  (`kill(-1, SIGKILL)` as that uid, then a `/proc` check, at most 50 × 20 ms), give the dir
  back to `agent:agent`, free the uid, and only then send the terminal frame. When the
  cleanup is incomplete (processes survive the bound, `/proc` cannot be read, or the give
  back fails), the terminal frame is the error `runner cleanup incomplete`, the uid is not
  used again until the runner restarts, its note stays, and the server logs a warning. A
  run with survivors stays `alive` for its tag, so its session is not resumed. Its dir is
  not walked then (a survivor could swap an entry during the walk): it keeps the run uid
  until the next start gives it back (below).
- **A server that dies mid-run.** Before the owner change the server writes a note
  (`/run/agento-runner/live/<uid>`, root-only, on the socket volume). At start it gives every
  noted dir back to `agent` before it serves, so the uid's next run never gets an old HOME.
  A note it cannot give back (an error, or a path the run-dir rule refuses) stays, and its
  uid stays out of the pool until a later start gives the dir back or an operator removes
  the note.
  The `cron` entrypoint, which gives `/workspace/artifacts` back to `agent` at start, never
  touches an entry of a uid of 20000 or more: that run may still be live in a runner.
- **`signal`** (pause) sends the signal to every process of the run's uid, so a descendant
  that left the process group is stopped too. The worker ignores `SIGTERM` (a handler,
  reset by exec for the CLI), so it still reports the stopped run.

**It needs a Linux file system on `/workspace`.** Docker Desktop on macOS mounts the host
directory with fake owners: every file shows `root`, and modes are not enforced, so the
runs work but are not kept apart. The per-run uid is a boundary only on a Linux host (or a
Linux volume).

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
The tests run as one ordinary user, so the privileged uid steps are fakes
(`tests/runner_fakes.py`) and an `execute` runs the real `worker.serve` in a thread. The
uid rules (ranges, pool, run-dir rule, cleanup order, held terminal frame, the toolbox gid
list) are in `tests/unit/framework/runner/test_run_uid.py`; its real-uid test runs only as
root on Linux.
