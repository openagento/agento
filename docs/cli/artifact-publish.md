# artifact:publish

Point an artifact's `current` at an existing version.

```bash
uv run bin/agento artifact:publish <artifact_code> <version_id> --expected <version_id> [--actor <who>]
```

| Flag | Meaning |
|---|---|
| `--expected <version_id>` | **Required.** The version you believe is current; the publish fails with `CURRENT_VERSION_CHANGED` if it changed |
| `--actor <who>` | Recorded in the audit row (default `admin`) |

`--expected` has no default on purpose. A default would silently disable the
optimistic-concurrency guard, which is the only thing that stops two publishers from
overwriting each other.

There is **no approval flag**: publication is a human running this command, or explicitly
asking the agent to run the tool.

## What it does, in order

1. Materializes the target version into the published tree (re-materializing it if
   retention pruned it). Idempotent — a failure here changes nothing and you retry.
2. The CAS on the store's `current` ref.
3. The atomic symlink swap, then retention.

On success it prints the version it published, the one it replaced, and the artifact's
preview URL.

## Repairing a stale served tree

If step 3 fails, the command prints a warning: the store moved and HTTP still serves the
previous version. The repair is this same command with `--expected` equal to the version
you just published:

```bash
uv run bin/agento artifact:publish demo-site v-20260905-154012-a3f2 --expected v-20260905-154012-a3f2
```

Steps 1 and 3 are idempotent and the CAS in step 2 becomes a no-op that *verifies* — it
succeeds precisely when `current` really is that version, and refuses with
`CURRENT_VERSION_CHANGED` otherwise. It is not a CAS bypass.

## How it runs

Host-local (`_LOCAL_MODULE_COMMANDS`), exec'ing into the toolbox:

```
docker compose <flags> exec -T toolbox node \
  /app/modules/core/versioned_artifacts/toolbox/cli.js --op publish --actor <who>
```

## Failures

Failures print as `Error: <ERROR_CODE>: <message>` and exit non-zero — never a traceback.
