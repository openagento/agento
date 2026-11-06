# artifacts:prune

Remove old run dirs and thread dirs under `workspace/artifacts/`. Sessions in the
agent_view's `state/` are never removed.

```bash
uv run bin/agento artifacts:prune [--dry-run] [--print] [--days N | --before YYYY-MM-DD]
```

Shortcut: `ar:pr`. Cron runs it every night at 03:23 (`core_artifacts_retention`).

| Flag | Meaning |
|---|---|
| `--dry-run` | Remove nothing. Report what would go |
| `--print` | Print each file as it is removed (`removed <path>`), and each dir that is kept with its reason (`kept <dir> (<reason>)`). With `--dry-run`: each file that would go (`would remove <path>`) |
| `--days N` | Use N in place of `core/artifacts/retention_days` (default 30). `0` keeps everything |
| `--before YYYY-MM-DD` | Remove what ended before this date, 00:00 UTC, in place of the N days. A date after today (UTC) is refused. Not with `--days` |

For example, `artifacts:prune --dry-run --print --before 2026-10-01` lists what ended before
1 October.

What goes:

- **A run dir** `<ws>/<view>/<job>` whose job ended (a terminal status) more than N days ago
  (or before the `--before` date)
  (clock: `job.finished_at`). A dir with no job row goes by its own mtime. The job row is
  locked and its status read again while the dir goes, so a retry keeps it. Before the dir
  goes, what the run left in its persistent HOME paths is copied back into `state/`.
- **A thread dir** `_threads/<key>` whose thread has no open job and whose last job ended more
  than N days ago (or before the `--before` date), under the thread row lock
  ([threads.md → Retention](../architecture/threads.md#retention)).

What stays, with the reason in the summary:

| Reason | Meaning |
|---|---|
| `open` | The job (or a job of the thread) is not finished |
| `unknown_harness` | The job's harness is not enabled, so its persistent paths are unknown |
| `unsaved` | `state/` cannot take a file of the run (a conflict); the warning names it |
| `copy_back_failed` | The copy-back failed with an OS error |
| `permission` | The dir cannot be removed (for example a run uid still owns a file) |

The last line is a summary:

```
[dry-run: ]run_dirs=12 thread_dirs=3 bytes=914358272 kept=open:2,unsaved:1
```

`--dry-run` skips the copy-back, so it can list a dir that a real run keeps (`unsaved`).

One run at a time: a run that finds the `artifacts_prune` lock held prints
`another artifacts:prune is running` and exits 0.

**Upgrade from 1.x.** The first run after the upgrade may remove a large amount of data. Each
run dir made since 1.0 holds a full copy of `state/`. Its sessions are already in `state/`, so
the copy-back has nothing to add and the dir goes. A run dir made before 1.0 links its
`.claude/projects` to `state/`; the prune never follows a link, so `state/` is safe. Run
`artifacts:prune --dry-run` first to see the size.
