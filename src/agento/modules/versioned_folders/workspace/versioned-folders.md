# Versioned folders

A **folder** is a named tree of files that publishes exactly one **version** at a time.
You never edit a folder directly. You open a **draft**, change files in it, freeze the
draft into an immutable version, and — only when asked — **publish** that version.

What you finalize is what you publish: a version never changes after it is created.

## Vocabulary

| Term | Meaning |
|---|---|
| `folder_code` | The stable name of a folder, e.g. `openagento-website` |
| `draft_id` | A private, editable copy you are working in, e.g. `d-a83f21` |
| `version_id` | An immutable snapshot, e.g. `v-20260905-154012-a3f2` |
| `current` | The one version the folder publishes right now |
| `revision` | A short opaque id for one saved change inside a draft |

## The flow

```
get_current -> create_draft -> list_files / read_file -> apply_changes (repeat)
            -> diff -> finalize -> publish(version_id, expected_current_version=<from get_current>)
```

1. `versioned_folder_get_current` — read which version is live, and keep that
   `current_version`: you need it later.
2. `versioned_folder_create_draft` — `base_version` is `current` or an explicit `version_id`.
3. `versioned_folder_list_files` / `versioned_folder_read_file` — read the draft.
4. `versioned_folder_apply_changes` — one call carries all writes and deletes of one
   step, and either all of it is saved or none of it is. Send a short `message`. Name
   each path once: a path repeated, or written and deleted in the same call, is
   refused as `INVALID_PATH` because the batch would have two answers for it.
5. `versioned_folder_diff` — check what you changed before you freeze it.
6. `versioned_folder_finalize` — freeze the draft into a version. The draft is gone
   afterwards; the version is permanent.
7. `versioned_folder_publish` — make that version the live one.

`versioned_folder_discard_draft` throws a draft away without creating a version.
`versioned_folder_list_versions` lists versions, newest first, and tells you which one
is current.

## Publishing is a separate decision

**Do not publish unless the user explicitly asked you to publish.** Finalizing is safe —
it only records your work. Publishing changes what everyone sees.

`publish` requires `expected_current_version`: the version you believe is live, taken
from `versioned_folder_get_current`. If the folder moved on since you read it, publish
fails with `CURRENT_VERSION_CHANGED`. **Do not retry blindly.** Read `get_current`
again, decide whether your version is still the right thing to publish, and only then
publish again with the new expected value.

## Rollback

Rollback is just publishing an older version. Nothing is deleted.

## When something fails

Every failure gives you an `error_code`. The ones you will act on:

| Code | What to do |
|---|---|
| `CURRENT_VERSION_CHANGED` | Re-read `get_current`, decide again |
| `DRAFT_LOCKED` | Another operation on the same draft is running; wait and retry |
| `DRAFT_HAS_NO_CHANGES` | Your batch changes nothing — there is nothing to save |
| `DRAFT_NOT_FOUND` | The draft is finished or was discarded; create a new one |
| `FOLDER_ACCESS_DENIED` | You may not use this folder; ask the user |
| `INVALID_PATH` / `PATH_OUTSIDE_FOLDER` | Use a plain relative path inside the folder |
| `FILE_TOO_LARGE` / `FOLDER_TOO_LARGE` / `TOO_MANY_FILES` | The change exceeds a limit |

Paths are always relative to the folder root, always forward-slashed, and may not
contain `..` or start at `.git`. A backslash is refused rather than read as a
separator, and one file has exactly one name: `a/b.txt`, `a//b.txt` and `a/./b.txt`
are the same path, so naming two of them in one batch is refused.
