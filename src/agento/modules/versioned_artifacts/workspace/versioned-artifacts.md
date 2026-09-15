# Versioned artifacts

A **artifact** is a named tree of files that publishes exactly one **version** at a time.
You never edit an artifact directly. You open a **draft**, edit its files in your own
workspace with the normal file tools, save the draft as an immutable version, and
**publish** that version.

You own the whole loop and need nobody to start it for you: create an artifact, draft it,
save versions, hand the `artifact_code` and a `version_id` to another agent to review or
extend, publish, draft again.

What you save is what you publish: a version never changes after it is created.

## Vocabulary

| Term | Meaning |
|---|---|
| `artifact_code` | The stable name of an artifact, e.g. `openagento-website`. Lowercase letters, digits and hyphens only — no uppercase, no underscore. `init` may hand back a different one; that one is the artifact |
| `draft_id` | A private, editable copy you are working in, e.g. `d-a83f21` |
| `version_id` | An immutable snapshot, e.g. `v-20260905-154012-a3f2` |
| `current` | The one version the artifact publishes right now |
| `revision` | A short opaque id for one saved change inside a draft |
| `path` | The directory in YOUR workspace where a draft or version is copied |

## Creating one

`versioned_artifact_init` makes a new, **empty** artifact. It takes an `artifact_code` and
an optional `title`, and nothing else — you fill version 1 the same way you make any other
change: `create_draft`, write files, `save_version`.

The code is lowercase letters, digits and hyphens: `quarterly-report`, never
`QUARTERLY_REPORT`. Every tool here takes the same shape. The code is also the URL, so
pick it for a reader.

**Use the code the answer gives back, not the one you asked for.** The name you send is a
wish. If another agent already took it you get the next free one — you ask for
`quarterly-report` and may get `quarterly-report-2`. Everything after that call — drafts,
versions, publishing, the URL you tell anyone — uses the code in the response. There is no
error to handle here; there is a name to read.

You own what you create, so no administrator has to grant you anything first.

`ARTIFACT_LIMIT_REACHED` means you already hold as many artifacts as you may. Nothing
deletes an artifact, so reuse one you made earlier — a new version, not a new artifact, is
how work moves forward.

## The flow

```
init (once) -> get_current -> create_draft -> edit files under the returned `path` (repeat)
            -> save_version -> diff -> publish(version_id, expected_current_version=<from get_current>)
            -> discard_draft
```

1. `versioned_artifact_get_current` — read which version is live, and keep that
   `current_version`: you need it later.
2. `versioned_artifact_create_draft` — `base_version` is `current` or an explicit
   `version_id`. It answers a `path`: a real directory in your workspace holding the
   draft's files.
3. **Edit that directory with the ordinary file tools.** Read, write, create and delete
   files there as you would anywhere else. The store sees nothing until you save.
4. `versioned_artifact_save_version` — copy the directory back and freeze it into a
   version. The draft **stays open**, so you can keep editing and save again.
5. `versioned_artifact_diff` — **`diff` reads the SAVED draft, not your workspace.**
   Edits you have not saved are invisible to it, so save first and then diff to see what
   the version you are about to publish actually contains.
6. `versioned_artifact_publish` — make that version the live one.
7. `versioned_artifact_discard_draft` — close the draft when you are done with it.

`versioned_artifact_materialize` copies a draft or a version into your workspace again —
use it to read an older version, or to recover after a failed step. It **replaces**
whatever is in that directory, so save first if you have unsaved edits.

`versioned_artifact_list` shows the artifacts you may use — the ones you created and the
ones you were granted — with their current version and their open drafts. `versioned_artifact_list_versions` lists versions, newest first, and
tells you which one is current.

## Saving twice changes nothing

`save_version` on an unchanged draft returns the version you already saved, not a new
one. A retry after a failure is safe.

## Publishing is a separate decision

**An artifact you created yourself is yours to publish** — that is the normal end of the
loop, and you do not need to ask.

**An artifact someone granted you is not.** Do not publish one unless the user explicitly
asked you to. Publishing changes what everyone already reading it sees.

Either way, know what saving does: **a saved version is already readable over HTTP** at its
own `preview_path`. Publishing only decides which version the artifact's main address shows.
Do not save anything to an artifact you would not want read.

`publish` requires `expected_current_version`: the version you believe is live, taken
from `versioned_artifact_get_current`. If the artifact moved on since you read it, publish
fails with `CURRENT_VERSION_CHANGED`. **Do not retry blindly.** Read `get_current`
again, decide whether your version is still the right thing to publish, and only then
publish again with the new expected value.

## Previewing in a browser

Saved versions are also written out as plain files a browser can open, and a small web
server answers them. You cannot open the page yourself — the server is on no network your
container can reach. Give the address to the person who asks; never say you checked it.

- `save_version` answers `preview_path` — a path like `/demo-site/v/v-20260905-154012-a3f2/`.
  It is `null` when the preview could not be written; the version itself is still saved.
- `get_current` answers `preview_url` — the full address of whatever the artifact
  publishes right now.
- `list_versions` answers `preview_path` per version, or `null` when an old preview was
  cleaned up. That version is still there: `materialize` it and you get every file back.

If `publish` answers `preview_stale: true`, the version is published but the site still
shows the previous one. Tell the user; an administrator repairs it.

## Rollback

Rollback is just publishing an older version. Nothing is deleted.

## When something fails

Every failure gives you an `error_code`. The ones you will act on:

| Code | What to do |
|---|---|
| `CURRENT_VERSION_CHANGED` | Re-read `get_current`, decide again |
| `DRAFT_LOCKED` | Another operation on the same draft is running; wait and retry |
| `DRAFT_NOT_FOUND` | The draft was discarded; create a new one |
| `DESK_MISSING` | The draft's directory is gone from your workspace; `materialize` it again |
| `WORKSPACE_UNAVAILABLE` | This session names no run, so it has no workspace of its own; you cannot use drafts here |
| `ARTIFACT_ACCESS_DENIED` | You may not use this artifact — it belongs to someone else. Ask the user |
| `ARTIFACT_LIMIT_REACHED` | You hold as many artifacts as you may; save a new version of one you already have |
| `INVALID_PATH` / `PATH_OUTSIDE_ARTIFACT` | A file in the directory is not a plain relative path inside it |
| `SYMLINK_NOT_ALLOWED` | Remove the symlink; save copies regular files only |
| `FILE_TOO_LARGE` / `ARTIFACT_TOO_LARGE` / `TOO_MANY_FILES` | The change exceeds a limit |

A save reads only regular files. A `.git` directory in your working copy is ignored,
and a symlink is refused rather than followed.
