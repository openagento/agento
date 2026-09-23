# Option B — a distinct OS uid per agent_view

Standing design notes. **Not a plan and not implemented.** Written 2026-08-23, while Option A
(keeping the SSH private key off disk entirely) was the accepted scope for the SSH-key incident.
Option A ships with a dated waiver of `RULES.md:112`; **Option B is the change that removes the
need for that waiver.** Everything below was verified against the tree at
`135cdfede8e5d04a2a498fbd06665dcd44d28c93`; re-verify the line numbers before using them.

---

## 1. Why B exists

The production incident had two halves. Option A closes one of them.

| What the attacker did | Closed by A? |
|---|---|
| Found and stole a persistent SSH private key file from a peer view's build tree, kept across 5 build generations, and pushed to that view's remote with it | **Yes** — the key never becomes a filesystem object |
| Read a peer view's whole tree, incl. `grep -rIl -e ghp_ -e github_pat_ /workspace/build/<workspace>` | **No** — same uid, still fully readable |
| Could use a peer view's live `ssh-agent` socket for the duration of that view's run | **No** — AC4-residual (3) |
| Could read a peer view's key out of `/proc/<pid>/environ` in the milliseconds before `exec` | **No** — AC4-residual (1) |
| Could read a peer view's key from the inherited descriptor `/proc/<inner-bash-pid>/fd/3` during the `ssh-add` bootstrap (`ssh-agent` execs the inner shell without closing fd 3) | **No** — AC4-residual (2) |

The single root cause behind all four rows: **every agent_view runs as one uid (`agent`) in one cron
container on one shared `/workspace` mount.** Under one uid, no file mode, path, or delivery channel
is an authorization boundary. B changes the uid; that is the only thing that changes the answer.

## 2. Verified facts about the current design

Established by reading the tree, not by assumption:

**One uid, and it is deliberately the host's uid**
- `docker/sandbox/Dockerfile:48-62` — `ARG HOST_UID=501`; `useradd -m -s /bin/bash -u ${HOST_UID} -g ${HOST_GID} agent`, and if that uid already exists the existing account is *renamed* to `agent`.
- `docker/toolbox/Dockerfile:48-60` — the same dance, so toolbox and cron share the uid.
- `docker/cron/Dockerfile:2` — `FROM ${SANDBOX_IMAGE}`, so cron inherits the sandbox user.
- `cli/install.py:168`, `cli/_provisioning.py:488-494` — `HOST_UID`/`HOST_GID` are captured at install and passed as build args.
- **Consequence:** container uid == host uid is *load-bearing* for the bind-mounted `/workspace` (the host user can read and edit the workspace directly). B gives that up, or keeps it only for one "primary" view.

**Nothing sets uid at spawn time**
- `docker/cron/entrypoint.sh:54,65` — `setup:upgrade` and the consumer both run via `su - agent -c …`. So **the consumer is already unprivileged**; it has no privilege left to drop.
- `docker/sandbox/entrypoint.sh:8` — `exec gosu agent "$@"`. The Dockerfile sets no `USER`, so a direct `docker exec` lands as **root** (this is why Option A dropped its `docker exec … kill <pid>` idea).
- `harness/subprocess_runner.py:145-165` (`_execute_process`) — the consumer starts the agent with a plain `subprocess.Popen(spawn_cmd, …, cwd=…, env=env)`. No `preexec_fn`, no `setuid`, no `--user`. This is the **consumer-side** choke point where a uid drop would go, and where `wrap_with_ssh_prelude(cmd)` already wraps the command.
- **There is a SECOND spawn path, and it is not this one — and BOTH forms of a local `agento run` take it.** It builds a `docker compose exec -u agent … sandbox <wrapped cmd>` argv: the interactive form adds `-it` and `os.execvp("docker", …)`s it, the headless form adds `-T` and runs the same argv shape through `subprocess.run` (`framework/cli/run.py:145,174,188`). Neither is "the interactive path" — the `-u agent` argument is common to both. So the agent runs INSIDE the sandbox container, as the container's `agent` user — the uid comes from `docker exec -u`, not from the Python process, and it never passes through `_execute_process`. **B must change both**: the consumer's `Popen` (drop to the view's uid) and the `-u agent` argument (pass the view's user). A B implementation that only fixes `_execute_process` leaves every local run on the shared `agent` uid.

**The workspace layout is already per-view**
- `framework/workspace_paths.py` — `theme/`, `build/`, `artifacts/` under `/workspace`.
- `framework/artifacts_dir.py:22-25` — `artifacts/<workspace_code>/<agent_view_code>/<run_id>`.
- `framework/artifacts_dir.py:62-69` — `build/<workspace_code>/<agent_view_code>/current` → a generation dir.
- `framework/persistent_home.py:11-31` — `build/<workspace_code>/<agent_view_code>/state`.
- **Consequence:** B is a `chown`/`chmod` pass over an existing tree, **not** a restructure. There is no shared-per-view directory that has to be split.

**The copy/symlink strategy survives the uid split**
- `framework/artifacts_dir.py:74-110` (`copy_build_to_artifacts_dir`) — adapter-owned files and `_UNIVERSAL_COPY_FILES = {CLAUDE.md, AGENTS.md, SOUL.md, .gitconfig}` are **copied**; every other entry is `dest.symlink_to(item.resolve())`.
- Every such symlink points into **the same view's** build tree (`get_current_build_dir` is called with that view's codes). Symlink permission checks apply to the **target at access time, with the accessor's uid** — same uid on both ends, so nothing breaks.
- `.gitconfig` is copied specifically so a run-time `git config` write cannot follow a symlink back into the shared build dir — the same reasoning B generalizes.
- `framework/persistent_home.py:34-54` (`link_persistent_paths`) symlinks declared HOME paths to that view's `state/` — again same-view, same-uid.
- A peer view following a symlink into another view's tree fails on the **target**, which is exactly the wanted outcome.

**Cross-uid pain already exists and is already handled once**
- `framework/artifacts_dir.py:28-47` — `prepare_artifacts_dir` has a whole `PermissionError` branch that logs owner uid/gid/mode and tells the admin to `sudo chown -R`. That branch exists because a toolbox-run-as-root era left files the `agent` uid could not delete. **B multiplies this failure class**, so treat that handler as a preview of the work.
- `docker/cron/entrypoint.sh:16-28` — a recursive `chown -R agent:agent /workspace/artifacts` heal at boot, precisely for "stale UIDs from prior builds".

## 3. Three variants

### V0 — keep one uid, take the credential store away from it (folded into AG-42)

**Status, 2026-08-25:** the owner decided residual channel (6) is closed by **AG-42** (Option B), not
by shipping V0 separately. What follows is therefore mechanism notes for AG-42 to draw from, not a
planned standalone step. One of its four parts is already **done** and must not be redone (the
non-dumpable process, below).

It closes residual channel (6) without per-view uids; it does **not** stop views reading each other's
artifacts. Four parts, all of them ordinary Linux:

- **No world-readable env file.** `/opt/cron-agent/env` exists only because `su - agent` wipes the
  environment for the crontab (`cron/entrypoint.sh:63`, mode 0644). Install the crontab for **root**
  instead of `agent` and give each line a **root-owned 0600** launcher that sources the secrets and then
  drops to `agent` (`setpriv --reuid agent --regid agent --init-groups`). Nothing an agent-uid process
  can read ever holds the store.
- **A non-dumpable consumer — DONE 2026-08-25, `framework/process_hardening.py`.**
  `prctl(PR_SET_DUMPABLE, 0)` makes the kernel reparent the process's `/proc` entries to root, so
  `environ`, `mem` and `fd` become unreadable to the same uid. It is called at **import time in
  `framework/cli/__init__.py`**, not in the consumer: every cron-spawned `bin/agento` job also sources
  the env file, so all of them need it, and reaching `main()` costs ~200 ms with the environ readable.
  Measured window from `execve` to the call: **40-60 ms**. This closed D-SSH-1 residual (4) (the
  heap) and the `/proc` route into (6); the 0644 env file is untouched, so the store capability itself
  is unchanged — that is the part below.
- **A root-owned gateway for the commands that need the store.** `agent_view:prepare-run` and friends
  must then run as root (or a `cron_admin` uid), reached from the host through the proxy rather than
  from the agent's uid. That is what finally makes an authorization check on those commands meaningful.
- **Tests that would fail today:** from an agent-uid process, assert it cannot find a readable file
  containing `AGENTO_ENCRYPTION_KEY`, and cannot connect to MySQL with store credentials. (The third —
  cannot read the consumer's environ — **passes** since 2026-08-25.) The live check that must flip from
  pass to fail is `agent_view:prepare-run <peer-view>` run as uid `agent`: today it prints a resolved
  SSH key.

**One constraint AG-42 inherits:** the env file cannot just be deleted. Both the crontab and the
consumer start as `su - agent -c "set -a; source /opt/cron-agent/env; set +a; …"`
(`docker/cron/entrypoint.sh:87,96`), so the secrets must move into a root-owned launcher before that
file can go — which is why this is a container permission-model change and not a config tweak.

Cost: the cron entrypoint, the crontab installation in `setup:upgrade`, and one new launcher script.
No per-view users, no ownership migration across `/workspace`.

### V1 — one container, uid dropped at the spawn (recommended for B itself)

- `setup:upgrade` (or the cron entrypoint) creates `agent_v<agent_view_id>` per view, plus a group per view.
- The consumer prefixes the harness spawn with `setpriv --reuid agent_v<N> --regid <N> --init-groups` (or `gosu`) at `subprocess_runner._execute_process`, the same choke point as `wrap_with_ssh_prelude`.
- Per-view dirs `0750 agent_v<N>:agent_v<N>`; the parents (`build/<ws>`, `artifacts/<ws>`) `0711 root` — traverse, no listing.
- `theme/` stays shared and world-readable (no secrets by design).
- **The catch, and it is the design's core tradeoff:** the consumer runs as `agent` today (`cron/entrypoint.sh:65`), so it cannot change uid. V1 requires the consumer to run as root, or to hold `CAP_SETUID`/`CAP_SETGID`, and to drop per run. Magento parallel: the `php-fpm` master runs as root *so that* it can fork workers as `pool-web1`, `pool-web2`.

### V2 — one sandbox service per view, `user:` in compose

- Docker enforces the boundary; the consumer stays unprivileged.
- Machinery partly exists: `docker-compose.yml` is already regenerated on `install`/`upgrade`/`module:enable` (`cli/templates/docker-compose.yml`), so "regenerate on agent_view change" fits the pattern.
- Cost: N containers, N restarts, and job dispatch must choose a container. Triples the container surface.

## 4. What needs a privilege it does not have today

This list is the real cost of B. Each item is a concrete code site.

1. **The builder must chown the finished generation** to the view uid → `CAP_CHOWN`, i.e. a root phase in the build or a small setuid helper. (`modules/workspace_build/src/builder.py`, at the end of `execute_build` / next to `gc_old_builds`.)
2. **`prepare_artifacts_dir`'s `shutil.rmtree`** of a view-owned stale run dir — the caller must either be the view uid or hold `CAP_DAC_OVERRIDE`. See the existing `PermissionError` branch (`artifacts_dir.py:28-47`).
3. **The toolbox writes into per-run artifacts dirs** (screenshots, videos, session scratch) as its own uid → a cross-uid write. Needs per-view groups with the toolbox as a member plus setgid dirs, or a toolbox-owned subdir per run. Decide which before anything else; it constrains the whole permission scheme.
4. **The boot heal `chown -R agent:agent /workspace/artifacts`** (`cron/entrypoint.sh:28`) must become per-view aware, and every existing deployment needs a **one-time ownership migration**. Precedent for the shape: the `workspace:ssh-purge` admin command from Option A.
5. **`gc_old_builds`** removing another uid's generation — same class as (2).

## 5. Constraints to settle, not assume

- **The credential store is readable by the shared uid, and B is what closes it.** The cron container
  holds `MYSQL_*` and `AGENTO_ENCRYPTION_KEY`, and its entrypoint writes them to a mode-0644 file
  (`/opt/cron-agent/env`) because `su - agent` wipes the environment for the crontab. Any process
  running as `agent` — every agent_view's run — can source that file, read `credential` /
  `core_config_data` and decrypt **any** view's secret, `agent_view:prepare-run <peer-view>` being the
  shortest route. The spawn environment no longer carries those names
  (`framework/credential_store_env.py`), but that is a reduction only: the file and
  `/proc/<consumer-pid>/environ` remain. So B must decide **who owns and who may read the cron env
  file and the consumer's environment** once views have their own uids — a per-view uid that can still
  read the store buys nothing here. This is DECISIONS.md D-SSH-1 residual channel (6), recorded and
  explicitly NOT waived.

- **`HOST_UID` ergonomics are lost.** Today container uid == host uid, so the bind-mounted workspace is directly usable from the host. Per-view uids break that. Options: keep one view on `HOST_UID`; add the host user to every view group; or accept `sudo` for host-side inspection. This is a product decision, not a detail.
- **Bind-mount ownership behaves differently per platform.** Docker Desktop on macOS (virtiofs, the dev setup here) does not honour uid ownership the way Linux prod does. **Verify on both targets before designing around it** — do not assert behaviour that has not been tested.
- **A local `agento run` — both forms — executes INSIDE the sandbox container** as the container `agent` user, via `docker compose exec -u agent` (`framework/cli/run.py:174-188`); only the *driving* CLI runs on the host as the host user. So this path is inside the boundary B moves, and it must carry the per-view user in its `-u` argument. What stays outside the boundary is the host CLI process: cron resolves the values and returns them in the `prepare-run` JSON, and the host CLI holds them for the life of the command (headless and interactive alike) to satisfy the name-only `-e KEY`. A bare `docker exec` without `-u` also lands as root.
- **`AGENTO_CONSUMER_MAX_WORKERS`** concurrency: concurrent runs of *different* views now run as different uids inside one process tree. Check nothing in the consumer assumes one uid owns everything it created (logging dirs under `/app/logs`, `chown -R agent /app/logs` at `cron/entrypoint.sh:21`).
- **`docker exec` is root** (`sandbox/entrypoint.sh:8` + no `USER`), so admin debugging still crosses every boundary. Intended, worth stating.
- **The framework must stay agent-agnostic** (CLAUDE.md): the uid drop belongs in the framework spawn path, driven by the `agent_view` record — never in a harness module, and with no `if harness == …` branch.

## 6. What B does *not* fix

- `docker exec`-level access without `-u` (root) — by design.
- The host CLI process that drives `agento run` (headless or interactive): cron resolves the values,
  the `prepare-run` JSON returns them, and that process holds them for the life of the command, on the
  admin's own machine, outside any container boundary.
- Anything about the *provider API keys* the `env` channel already carries into the host process — for either local run form.
- The toolbox's own credential store — that is already the correctly-confined side.

## 7. Ready-to-use prompt for the B loop

Start it only after Option A has landed (B assumes Phase 1-3 of A is in the tree — there is no
per-build key left to own). Paste this as-is:

```
/loop /loop-skill implement Option B: a distinct OS uid per agent_view, so that one agent_view cannot
read another view's files, credentials or ssh-agent socket.

Context and constraints are in docs/architecture/option-b-per-view-uid.md — read it first; every
fact in it was verified against the tree and it names the code sites, the privilege list and the open
questions. Do not re-derive them.

Scope:
- Per-view OS uid/gid created from the agent_view record, and the harness spawn dropping to it at the
  consumer choke point in harness/subprocess_runner.py:_execute_process AND the `-u` argument of the
  local-run `docker compose exec` path in framework/cli/run.py — both forms share its `-u` argument
  (V1 in that doc). Justify V1 vs
  V2 explicitly in the plan; V1 is the recommendation.
- Ownership and modes across build/, artifacts/ and state/ per that doc's section 3.
- Every item in section 4 (privilege list) must be addressed or explicitly deferred with a reason:
  builder chown, prepare_artifacts_dir rmtree, toolbox cross-uid writes, the boot chown heal plus a
  one-time ownership migration command for existing deployments, gc_old_builds.
- Settle each constraint in section 5 in the plan, including the HOST_UID ergonomics decision and a
  statement of what was actually tested on macOS/Docker Desktop vs Linux.

Acceptance criteria:
1. With two agent_views and a job running in each, view A cannot read, list, or stat any file under
   view B's build/, artifacts/ or state/ tree, and cannot use view B's SSH_AUTH_SOCK. Demonstrate by
   replaying the incident's sequence: find /workspace -name 'id_rsa*',
   grep -rIl -e ghp_ -e github_pat_ /workspace/build, and an attempted ssh -T with the peer's socket.
2. The copy/symlink build strategy still works unchanged for the owning view (same-view symlinks
   resolve; per-run copies stay private).
3. Existing deployments migrate with one documented admin command; a partly-migrated tree fails loudly
   rather than silently running with the old ownership.
4. bin/test green, and the RULES.md:112 waiver recorded for Option A in DECISIONS.md is retired or
   narrowed by this change, with the entry updated to say so.
5. The framework stays agent-agnostic: no harness-specific branch anywhere in the uid path.
```

Reviewer settings that worked for the A loop: `codex` / `gpt-5.6-sol` / effort `high`, session reuse
on, and expect the reviewer to hold the same `RULES.md:112` line — B narrows the waiver but does not
delete it (root `docker exec` without `-u`, and the host CLI process driving `agento run` in either
form, remain).
