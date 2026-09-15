import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { mkdir, rm, stat, writeFile, unlink, readFile, rename } from 'node:fs/promises';
import { runGit } from './git-exec.js';
import { ArtifactError, GitFailure, ERROR_CODES } from './errors.js';
import {
  validateArtifactCode, validateDraftId, validateVersionId, ARTIFACT_CODE_RE,
  safeResolve, listDirOrEmpty, artifactRoot, repoDir, draftDir, validateRelPath,
  selectorSourceId,
} from './paths.js';
import { mirrorIn, mirrorOut, emptyDesk } from './desk-io.js';

// The hardening flags every invocation carries. `core.hooksPath=/dev/null` is
// PRD §32's "hooks disabled"; `core.symlinks=false` makes a symlink in a tree
// materialize as a plain file rather than a link, so a crafted tree cannot
// create one on checkout. Attributes are NOT hardened here: `core.attributesFile`
// has LOWER precedence than an in-tree `.gitattributes`, so the agent's committed
// file would still win — see ATTRIBUTES below, which uses the one location that
// outranks the tree.
const HARDEN = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.symlinks=false'];

// A tree the agent controls must not decide how Git reads that tree. An agent who
// commits a `.gitattributes` can otherwise make every reading path transform bytes:
// `text eol=crlf` mangles a draft checkout, `export-ignore` drops a file from an
// archive, `export-subst` rewrites content, `ident` rewrites `$Id$`, and a `diff`
// attribute can force "Binary files differ". `$GIT_DIR/info/attributes` has the
// HIGHEST precedence in Git's lookup and the bare repo is toolbox-only, so this is
// the one place the agent cannot reach.
//
// `!diff` and `!working-tree-encoding` are Unspecified (auto-detect), NOT Unset:
// `-diff` would tell Git the file IS binary and every diff would read
// "Binary files a/f.txt and b/f.txt differ". `-ident` is correct because `ident`
// is a plain boolean whose Unset state is "do not expand $Id$".
const ATTRIBUTES = '* -export-ignore -export-subst -text -eol -filter !diff -ident !working-tree-encoding\n';

/** Write `info/attributes` when it is absent or has drifted. Idempotent, one
 *  `readFile` per call, and `rename` so a concurrent reader never sees a partial
 *  file. Written on EVERY store open, not at init only: a store created before
 *  this existed would otherwise stay permanently unprotected. */
async function neutralizeAttributes(repo) {
  const file = path.join(repo, 'info', 'attributes');
  if (await readFile(file, 'utf8').then((c) => c === ATTRIBUTES, () => false)) return;
  const tmp = `${file}.${randomBytes(6).toString('hex')}.tmp`;
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(tmp, ATTRIBUTES);
  await rename(tmp, file);
}

// TWO helpers, and the split is not stylistic:
//   $ git --git-dir <bare> -C <worktree> status
//   fatal: this operation must be run in a work tree
// An explicit `--git-dir` at a BARE repository tells Git there is no work tree,
// and that verdict wins over `-C`. The worktree does not need `--git-dir`: the
// `.git` file `worktree add` writes points back at the repository.
function gitBare(gitDir, args, opts = {}) {
  return runGit([...HARDEN, '--git-dir', gitDir, ...args], opts);   // throws GitFailure, NOT ArtifactError
}
function gitWork(worktreeDir, args, opts = {}) {
  return runGit([...HARDEN, '-C', worktreeDir, ...args], opts);     // same contract
}

const REF_CURRENT = 'refs/agento/current';
const versionRef = (id) => `refs/agento/versions/${id}`;
const draftBranch = (id) => `refs/heads/agento-drafts/${id}`;
const baseRef = (id) => `refs/agento/draft-bases/${id}`;
const discardingRef = (id) => `refs/agento/discarding/${id}`;

// The owner marker inside an artifact root. A plain name, next to `repo.git`,
// `worktrees/` and `locks/`; an artifact_code cannot collide with it because the
// marker lives INSIDE the artifact, not beside it.
const OWNER_FILE = 'owner';

const MAX_ID_RETRIES = 5;
const DEFAULT_MAX_OUTPUT = 64 * 1024 * 1024;
const DEFAULT_MAX_DIFF_BYTES = 256 * 1024;

const out = (r) => r.stdout.toString('utf8');
/** Absence is ENOENT and nothing else. EACCES, ENOTDIR, EIO and EMFILE all say
 *  "the answer is unknown", and answering them with `false` is what lets a
 *  damaged or unreadable store be reported as ARTIFACT_NOT_FOUND, DRAFT_NOT_FOUND
 *  or a completed teardown — the same defect class as trusting `rev-parse`'s
 *  exit code below, one layer down. */
const exists = async (p) => {
  try { await stat(p); return true; }
  catch (err) {
    if (err?.code === 'ENOENT') return false;
    throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the artifact store could not be read', { cause: err });
  }
};

/** A failed `rev-parse` is NOT evidence that a ref is absent — it is equally what a
 *  dangling or unreadable ref produces, and reporting a damaged store as "nothing
 *  published" is the worse of the two failures. `for-each-ref` separates the cases
 *  without matching Git's message text, which is neither stable nor locale-proof:
 *
 *    absent    -> exit 0, no output,   no stderr
 *    dangling  -> exit 0, prints a sha (that `rev-parse` could not peel)
 *    unreadable-> exit 0, no output,   "warning: ignoring broken ref ..." on stderr
 *
 *  Only the first is absence; the other two mean the store is damaged. */
async function assertRefAbsent(repo, ref, cause) {
  let probe;
  try {
    probe = await gitBare(repo, ['for-each-ref', '--format=%(objectname)', ref], { maxBytes: 4096 });
  } catch (err) {
    throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the artifact store could not be read', { cause: err });
  }
  if (out(probe).trim() !== '' || probe.stderr.trim() !== '') {
    throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the artifact store is damaged', { cause });
  }
}

async function readRef(repo, ref) {
  try {
    const r = await gitBare(repo, ['rev-parse', '--verify', '--quiet', `${ref}^{commit}`], { maxBytes: 4096 });
    return out(r).trim() || null;
  } catch (err) {
    if (!(err instanceof GitFailure)) throw err;
    await assertRefAbsent(repo, ref, err);
    return null;
  }
}
const refExists = async (repo, ref) => (await readRef(repo, ref)) !== null;

function defaultVersionId() {
  const d = new Date();
  const p = (n, w = 2) => String(n).padStart(w, '0');
  const stamp = `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}` +
    `-${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}`;
  return `v-${stamp}-${randomBytes(2).toString('hex')}`;
}

const NOOP_HOOKS = { afterWorktree: () => {}, beforeTeardown: () => {}, afterTeardownStep: () => {} };

export function createBackend({ newVersionId = defaultVersionId, hooks = {} } = {}) {
  const H = { ...NOOP_HOOKS, ...hooks };

  // ---------------------------------------------------------------- helpers

  async function requireArtifact(storageRoot, artifactCode) {
    const root = artifactRoot(storageRoot, artifactCode);
    if (!(await exists(root))) throw new ArtifactError(ERROR_CODES.ARTIFACT_NOT_FOUND, 'artifact not found');
    const repo = repoDir(storageRoot, artifactCode);
    await neutralizeAttributes(repo);
    return repo;
  }

  /** Delete a ref, tolerating "it was already gone" and NOTHING else. `update-ref -d`
   *  fails identically for an absent ref and for one it could not write, so the
   *  postcondition is checked rather than assumed. */
  async function deleteRefVerified(repo, ref, message) {
    try {
      await gitBare(repo, ['update-ref', '-d', ref]);
      return;
    } catch (err) {
      if (!(err instanceof GitFailure)) throw err;
      if (await refExists(repo, ref)) {
        throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, message, { cause: err });
      }
    }
  }

  /** Idempotent teardown, shared by discardDraft and createDraft's
   *  compensating cleanup. Every step is written to succeed on an already-done
   *  store, so a retry converges instead of needing a human. */
  async function teardownDraft(storageRoot, artifactCode, draftId) {
    const repo = repoDir(storageRoot, artifactCode);
    const dir = draftDir(storageRoot, artifactCode, draftId);
    await H.beforeTeardown(draftId);

    // Every step below is allowed to FAIL, but none of them is allowed to fail
    // SILENTLY: "already done" and "could not be done" produce the same exception,
    // and swallowing both is what lets a permission error report a draft as torn
    // down while its branch and base marker are still in the store. So each step
    // verifies its own postcondition and only then treats the failure as an
    // already-done step.
    try { await gitBare(repo, ['worktree', 'remove', '--force', dir]); }
    catch (err) { if (!(err instanceof GitFailure)) throw err; }   // "is not a working tree" — already gone
    try { await gitBare(repo, ['worktree', 'prune']); }
    catch (err) { if (!(err instanceof GitFailure)) throw err; }
    // Same postcondition rule as the refs below: the checkout's metadata must be
    // gone, whether this call removed it or an earlier attempt already had.
    if (await exists(path.join(repo, 'worktrees', draftId))) {
      throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the draft could not be closed');
    }
    await H.afterTeardownStep('worktree', draftId);

    await deleteRefVerified(repo, draftBranch(draftId), 'the draft could not be closed');
    await H.afterTeardownStep('branch', draftId);

    await deleteRefVerified(repo, baseRef(draftId), 'the draft could not be closed');
    await H.afterTeardownStep('baseRef', draftId);

    // `worktree remove` refuses a directory Git has no metadata for and `prune`
    // only touches metadata Git already has — so neither deletes a checkout that
    // exists on disk but was never registered. That is exactly what a crash
    // inside `worktree add` leaves, and what reconcileDrafts must clean up.
    await rm(dir, { recursive: true, force: true });
  }

  async function draftState(storageRoot, artifactCode, draftId) {
    const repo = await requireArtifact(storageRoot, artifactCode);
    validateDraftId(draftId);
    if (await refExists(repo, discardingRef(draftId))) return 'discarding';
    if (!(await exists(draftDir(storageRoot, artifactCode, draftId)))) return 'missing';
    if (!(await refExists(repo, baseRef(draftId)))) return 'incomplete';
    return 'open';
  }

  async function requireOpenDraft(storageRoot, artifactCode, draftId) {
    const state = await draftState(storageRoot, artifactCode, draftId);
    if (state !== 'open') throw new ArtifactError(ERROR_CODES.DRAFT_NOT_FOUND, 'draft not found');
    return state;
  }

  async function versionIdForCommit(repo, commit) {
    const r = await gitBare(repo, ['for-each-ref', '--format=%(refname:lstrip=3)%00%(objectname)',
      'refs/agento/versions/'], { maxBytes: DEFAULT_MAX_OUTPUT });
    const hits = out(r).split('\n').filter(Boolean)
      .map((line) => line.split('\0'))
      .filter(([, sha]) => sha === commit)
      .map(([id]) => id);
    if (hits.length === 1) return hits[0];
    if (hits.length === 0) return null;
    throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the version pointer is ambiguous');
  }

  /** Create-only, and it VERIFIES before classifying: a non-zero exit alone does
   *  not prove a collision — disk-full and EACCES exit non-zero too. */
  async function createVersionRef(repo, versionId, commit) {
    try {
      await gitBare(repo, ['update-ref', versionRef(versionId), commit, '']);
    } catch (err) {
      if (await refExists(repo, versionRef(versionId))) {
        throw new ArtifactError(ERROR_CODES.VERSION_ALREADY_EXISTS, 'version id already in use');
      }
      // The backend has no logger and must not acquire one — it is a pure adapter.
      // The GitFailure rides along as `cause`; the service logs it once.
      throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'could not record the version', { cause: err });
    }
  }

  /** EXACTLY ONE source. Both tools take `draft_id` and `version_id` as independently
   *  optional arguments, so a caller can name none or both; the contract names one.
   *  Naming both used to select the draft and IGNORE the version silently — the worst
   *  of the three outcomes, because the caller is told nothing and reads a file it did
   *  not ask for. Named nothing is refused here too, and refused BEFORE the store is
   *  touched: a malformed request must not depend on an artifact existing. */
  const treeishFor = (selector, storageRoot, artifactCode) => {
    selectorSourceId(selector);
    if (selector?.draftId) return draftBranch(validateDraftId(selector.draftId));
    return versionRef(validateVersionId(selector.versionId));
  };

  async function resolveSelector(storageRoot, artifactCode, selector) {
    selectorSourceId(selector);
    const repo = await requireArtifact(storageRoot, artifactCode);
    if (selector?.draftId) {
      await requireOpenDraft(storageRoot, artifactCode, selector.draftId);
    } else if (selector?.versionId) {
      if (!(await refExists(repo, versionRef(validateVersionId(selector.versionId))))) {
        throw new ArtifactError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found');
      }
    }
    return { repo, treeish: treeishFor(selector, storageRoot, artifactCode) };
  }

  /** The store → a desk, addressed by DESCRIPTOR. The caller's `openDesk` walk is the
   *  containment proof; re-deriving the desk path here would reopen the window it closed.
   *
   *  ONE path for both selectors: what a draft records is its branch tip, exactly as a
   *  version is its tag. The worktree is only where `saveVersion` stages the desk before
   *  it commits, both under one lock, so a worktree AHEAD of the tip is a torn write —
   *  and this call is what an agent reaches for to recover from one.
   *
   *  The extraction is `read-tree` + `checkout-index`, not `git archive` piped into
   *  `tar`. Both are byte-faithful — the `info/attributes` line above neutralizes
   *  `export-ignore`, `export-subst` and `text` — but they disagree on one entry kind:
   *  measured, `tar` writes a `120000` entry as a REAL symlink inside the store volume
   *  (including one pointing out of it), while `checkout-index` under this module's own
   *  `core.symlinks=false` writes a regular file holding the target text, which is how the
   *  draft worktree already checks out. It also needs no `tar` binary and no non-git exec
   *  path. `GIT_INDEX_FILE` keeps the scratch index out of the repository, which is bare
   *  and has none of its own. */
  /** Under `storageRoot` so the scratch shares the store volume and never the desk. The
   *  leading dot is what keeps it out of `listArtifacts`: `ARTIFACT_CODE_RE` requires a
   *  leading [a-z0-9], and the entry also carries no `repo.git`. */
  const scratchRoot = (storageRoot) => path.join(storageRoot, '.tmp');

  /** Boot-time only. `materialize` removes its own scratch in a `finally`, which a
   *  SIGKILL does not run — so what is left here at boot is, by definition, dead. */
  const sweepScratch = (storageRoot) => rm(scratchRoot(storageRoot), { recursive: true, force: true });

  /** The extraction itself, shared by the desk path and the published tree. It is here
   *  and not in `desk-io.js` because `mirrorIn` calls `requireLinux()` — a published
   *  directory is written by the toolbox itself and needs none of the desk's
   *  fd-anchoring. The index sits BESIDE the checkout, not in it — inside, `mirrorIn`
   *  would copy it onto the desk — and one `rm` of the parent still removes both.
   *  `GIT_INDEX_FILE` keeps it out of the repository, which is bare and has none. */
  async function extractTree(repo, treeish, scratch, dir) {
    const env = { GIT_INDEX_FILE: path.join(scratch, 'index') };
    await mkdir(dir, { recursive: true });
    await gitBare(repo, ['read-tree', treeish], { env });
    await gitBare(repo, ['--work-tree', dir, 'checkout-index', '-a', '-f'], { env });
  }

  /** The store -> the PUBLISHED tree, which the toolbox owns end to end: no descriptor,
   *  no `mirrorIn`, no desk. Extract into a scratch and `rename` the finished tree into
   *  place, so a reader never sees a half-written version directory.
   *
   *  Idempotent in both directions, which is what makes `publish` safely repeatable: an
   *  existing destination returns immediately, and a rename lost to a concurrent publish
   *  of the SAME version is not an error — the directory is immutable, so the loser only
   *  drops its scratch. */
  async function materializePublished(storageRoot, artifactCode, versionId, { destDir, scratchDir }) {
    if (await exists(destDir)) return false;
    const { repo, treeish } = await resolveSelector(storageRoot, artifactCode, { versionId });
    const scratch = path.join(scratchDir, randomBytes(8).toString('hex'));
    const tree = path.join(scratch, 'tree');
    try {
      await extractTree(repo, treeish, scratch, tree);
      await mkdir(path.dirname(destDir), { recursive: true });
      try {
        await rename(tree, destDir);
      } catch (err) {
        if (!(await exists(destDir))) throw err;
        return false;
      }
      return true;
    } catch (err) {
      if (!(err instanceof GitFailure)) throw err;
      throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the version could not be read', { cause: err });
    } finally {
      await rm(scratch, { recursive: true, force: true });
    }
  }

  async function materialize(storageRoot, artifactCode, selector, destFd) {
    const { repo, treeish } = await resolveSelector(storageRoot, artifactCode, selector);

    const scratch = path.join(scratchRoot(storageRoot), randomBytes(8).toString('hex'));
    const tree = path.join(scratch, 'tree');
    try {
      await extractTree(repo, treeish, scratch, tree);
      // The desk is emptied HERE, not by the caller: authorization, the selector and the
      // whole extraction have all succeeded by this line, so a refused or failed
      // materialize leaves the agent's unsaved work where it was.
      emptyDesk(destFd);
      mirrorIn(tree, destFd);
    } catch (err) {
      // The only backend read left, so it is also the only place a damaged store can
      // still reach a caller as a raw GitFailure. `mirrorIn` throws ArtifactError, so
      // one catch is enough to keep the contract "no GitFailure leaves the backend".
      if (!(err instanceof GitFailure)) throw err;
      throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the version could not be read', { cause: err });
    } finally {
      // Unconditional: a desk that refused the copy must not also leak a store tree.
      await rm(scratch, { recursive: true, force: true });
    }
  }

  // ------------------------------------------------------------------- init

  async function init(storageRoot, artifactCode, { files = [], allowSymlinks = false, limits = {}, owningView = null } = {}) {
    validateArtifactCode(artifactCode);
    const root = artifactRoot(storageRoot, artifactCode);
    if (await exists(root)) throw new ArtifactError(ERROR_CODES.ARTIFACT_ALREADY_EXISTS, 'artifact already exists');

    // Validate the WHOLE payload before creating anything: a half-created artifact
    // would make every retry report "already exists".
    const entries = [];
    let total = 0;
    for (const f of files) {
      if (f?.symlink) throw new ArtifactError(ERROR_CODES.SYMLINK_NOT_ALLOWED, 'symbolic links are not allowed in this artifact');
      // The CANONICAL path, from the validator that produced it. Re-deriving it with
      // `split(path.sep)` was a different function: on Linux `path.sep` is "/", so
      // "a//b.txt" and "a/./b.txt" kept their original spelling as the tree-entry
      // name while containment resolved them to "a/b.txt" — a tree entry `readFile`
      // could then not address, and two identifiers for one file.
      const rel = validateRelPath(String(f?.path ?? ''));
      await safeResolve(root, rel, { allowSymlinks });
      const buf = f?.encoding === 'base64'
        ? Buffer.from(String(f.content ?? ''), 'base64')
        : Buffer.from(String(f?.content ?? ''), 'utf8');
      if (limits.max_file_size != null && buf.length > limits.max_file_size) {
        throw new ArtifactError(ERROR_CODES.FILE_TOO_LARGE, 'file exceeds the size limit');
      }
      total += buf.length;
      entries.push({ path: rel, buf });
    }
    if (limits.max_files != null && entries.length > limits.max_files) {
      throw new ArtifactError(ERROR_CODES.TOO_MANY_FILES, 'too many files');
    }
    if (limits.max_total_size != null && total > limits.max_total_size) {
      throw new ArtifactError(ERROR_CODES.ARTIFACT_TOO_LARGE, 'artifact exceeds the total size limit');
    }

    const repo = repoDir(storageRoot, artifactCode);
    try {
      await mkdir(root, { recursive: true });
      await runGit(['init', '--bare', '--quiet', '--template=', repo]);
      await neutralizeAttributes(repo);

      // Import through a temporary index so no working tree is ever needed.
      // Walking explicitly rather than `git add` means a source .gitignore cannot
      // silently drop a file the administrator asked to import.
      const indexFile = path.join(root, 'import.index');
      const env = { GIT_INDEX_FILE: indexFile };
      for (const e of entries) {
        const h = out(await gitBare(repo, ['hash-object', '-w', '--stdin'], { input: e.buf, maxBytes: 4096 })).trim();
        await gitBare(repo, ['update-index', '--add', '--cacheinfo', `100644,${h},${e.path}`], { env, maxBytes: 4096 });
      }
      const tree = out(await gitBare(repo, ['write-tree'], { env, maxBytes: 4096 })).trim();
      // A temporary file inside the artifact being created, already read; a leftover
      // is inert and failing the import over it would be the worse outcome.
      await unlink(indexFile).catch(() => {});
      const commit = out(await gitBare(repo, ['commit-tree', tree, '-m', 'Initial import'], { maxBytes: 4096 })).trim();

      let versionId = null;
      let lastErr = null;
      for (let i = 0; i < MAX_ID_RETRIES; i += 1) {
        const candidate = newVersionId();
        try { await createVersionRef(repo, candidate, commit); versionId = candidate; break; }
        catch (err) {
          lastErr = err;
          if (err instanceof ArtifactError && err.code === ERROR_CODES.VERSION_ALREADY_EXISTS) continue;
          throw err;
        }
      }
      if (!versionId) throw lastErr;
      await gitBare(repo, ['update-ref', REF_CURRENT, commit, '']);
      // WHO may use this artifact, recorded in the store because nothing else carries
      // it: the code is a plain name, so it says nothing about ownership. Written
      // inside the guarded block, so the `rm -rf` below is what makes it atomic — the
      // artifact exists with its owner, or it does not exist. An artifact with no
      // marker is owned by nobody and is reachable only through `allowed_artifacts`,
      // which is exactly how a store predating this file behaves.
      if (owningView !== null) await writeFile(path.join(root, OWNER_FILE), `${owningView}\n`);
      return { artifact_code: artifactCode, current_version: versionId };
    } catch (err) {
      await rm(root, { recursive: true, force: true });
      throw err;
    }
  }

  /** The agent_view that created this artifact, or `null` for one that records none.
   *  Unreadable is `null` too: a membership check must not grant on a failed read. */
  async function readOwningView(storageRoot, artifactCode) {
    try {
      const raw = await readFile(path.join(artifactRoot(storageRoot, artifactCode), OWNER_FILE), 'utf8');
      const value = raw.trim();
      return value === '' ? null : value;
    } catch { return null; }
  }

  // ------------------------------------------------------- current, versions

  async function getCurrent(storageRoot, artifactCode) {
    const repo = await requireArtifact(storageRoot, artifactCode);
    const commit = await readRef(repo, REF_CURRENT);
    if (!commit) throw new ArtifactError(ERROR_CODES.VERSION_NOT_FOUND, 'this artifact has no published version');
    const versionId = await versionIdForCommit(repo, commit);
    if (!versionId) throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the current pointer does not name a version');
    return { artifact_code: artifactCode, current_version: versionId };
  }

  async function listVersions(storageRoot, artifactCode, { limit = 50 } = {}) {
    const repo = await requireArtifact(storageRoot, artifactCode);
    // `--count` is passed to GIT, not applied to the result: slicing afterwards
    // still reads and formats every ref, and an artifact gains one version per
    // publish forever. `%(refname:lstrip=3)` — NOT `%(refname:short)`, which for
    // a private namespace strips only `refs/` and would leak `agento/versions/…`
    // into the one field the agent round-trips back into publish.
    // Sorted by the version id, NOT by `creatordate`. These are lightweight refs, so
    // creatordate is the TARGET COMMIT's date — the commit the save made, which can
    // predate the save by any amount. Save an old draft after a newer one and
    // "newest first" listed it second, so `limit: 1` omitted the newest version.
    // The id embeds its own UTC save timestamp in a fixed-width form, so
    // descending refname IS descending save time. Ids minted in the same
    // second still tie — documented, and the random suffix breaks it arbitrarily.
    const r = await gitBare(repo, ['for-each-ref', `--count=${limit}`, '--sort=-refname',
      '--format=%(refname:lstrip=3)%00%(objectname)',
      'refs/agento/versions/'], { maxBytes: DEFAULT_MAX_OUTPUT });
    return out(r).split('\n').filter(Boolean).map((line) => {
      const [version_id, revision] = line.split('\0');
      return { version_id, revision };
    });
  }

  async function publish(storageRoot, artifactCode, versionId, expectedCurrentVersion) {
    const repo = await requireArtifact(storageRoot, artifactCode);
    validateVersionId(versionId);
    validateVersionId(expectedCurrentVersion);
    const targetCommit = await readRef(repo, versionRef(versionId));
    if (!targetCommit) throw new ArtifactError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found');
    const expectedCommit = await readRef(repo, versionRef(expectedCurrentVersion));
    if (!expectedCommit) throw new ArtifactError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found');

    try {
      // The three-argument form fails if the ref no longer equals <old> — the
      // atomic CAS PRD §19 requires.
      await gitBare(repo, ['update-ref', REF_CURRENT, targetCommit, expectedCommit]);
    } catch (err) {
      const actual = await readRef(repo, REF_CURRENT);
      if (actual !== expectedCommit) {
        throw new ArtifactError(ERROR_CODES.CURRENT_VERSION_CHANGED,
          'the published version changed; re-read the current version and decide again');
      }
      throw new ArtifactError(ERROR_CODES.PUBLISH_FAILED, 'could not publish the version', { cause: err });
    }
    return { previous_version: expectedCurrentVersion, current_version: versionId };
  }

  // ----------------------------------------------------------------- drafts

  async function createDraft(storageRoot, artifactCode, baseVersion, description) {
    const repo = await requireArtifact(storageRoot, artifactCode);
    let baseVersionId;
    let baseCommit;
    if (!baseVersion || baseVersion === 'current') {
      ({ current_version: baseVersionId } = await getCurrent(storageRoot, artifactCode));
      baseCommit = await readRef(repo, versionRef(baseVersionId));
    } else {
      baseVersionId = validateVersionId(baseVersion);
      baseCommit = await readRef(repo, versionRef(baseVersionId));
    }
    if (!baseCommit) throw new ArtifactError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found');

    const draftId = `d-${randomBytes(3).toString('hex')}`;
    const dir = draftDir(storageRoot, artifactCode, draftId);
    await mkdir(path.dirname(dir), { recursive: true });
    await gitBare(repo, ['worktree', 'add', '--quiet', '-b', `agento-drafts/${draftId}`, dir, baseCommit]);
    try {
      await H.afterWorktree(draftId);
      await gitBare(repo, ['update-ref', baseRef(draftId), baseCommit, '']);
    } catch (err) {
      // Compensating cleanup through the SAME idempotent teardown, so a cleanup
      // that itself half-fails leaves a draft the next call can finish removing.
      try { await teardownDraft(storageRoot, artifactCode, draftId); } catch { /* reclaimed at startup */ }
      throw err;
    }
    void description;
    return { draft_id: draftId, artifact_code: artifactCode, base_version: baseVersionId };
  }

  /** The open drafts of one artifact, with the version each was based on. The same walk
   *  `reconcileDrafts` uses, filtered the other way: it exists so a retried job whose desk
   *  was wiped can find the draft it left behind. */
  async function listOpenDrafts(storageRoot, artifactCode) {
    const repo = await requireArtifact(storageRoot, artifactCode);
    const open = [];
    for (const e of await listDirOrEmpty(path.join(artifactRoot(storageRoot, artifactCode), 'worktrees'))) {
      if (!e.isDirectory()) continue;
      try { validateDraftId(e.name); } catch { continue; }
      if (await draftState(storageRoot, artifactCode, e.name) !== 'open') continue;
      const baseCommit = await readRef(repo, baseRef(e.name));
      open.push({ draft_id: e.name, base_version: baseCommit ? await versionIdForCommit(repo, baseCommit) : null });
    }
    return open;
  }

  async function reconcileDrafts(storageRoot, artifactCode) {
    const root = artifactRoot(storageRoot, artifactCode);
    const reclaimed = [];
    for (const e of await listDirOrEmpty(path.join(root, 'worktrees'))) {
      if (!e.isDirectory()) continue;
      try { validateDraftId(e.name); } catch { continue; }
      if (await draftState(storageRoot, artifactCode, e.name) !== 'incomplete') continue;
      await teardownDraft(storageRoot, artifactCode, e.name);
      reclaimed.push(e.name);
    }
    return reclaimed;
  }

  /** Every artifact the store actually holds. Deliberately NOT the allowlist: the
   *  allowlist answers "may this caller reach that artifact", and startup reclamation
   *  is internal maintenance with no caller — an artifact that sits in no agent_view's
   *  allowlist is exactly where an orphan would otherwise stay forever. A directory
   *  that is not a well-formed artifact_code, or holds no store, is not an artifact. */
  async function listArtifacts(storageRoot) {
    const artifacts = [];
    for (const e of await listDirOrEmpty(storageRoot)) {
      if (!e.isDirectory() || !ARTIFACT_CODE_RE.test(e.name)) continue;
      if (!(await exists(repoDir(storageRoot, e.name)))) continue;
      artifacts.push(e.name);
    }
    return artifacts.sort();
  }

  const getDraftPath = (storageRoot, artifactCode, draftId) => draftDir(storageRoot, artifactCode, draftId);

  async function commitDraft(storageRoot, artifactCode, draftId, message = '', trailers = {}) {
    await requireArtifact(storageRoot, artifactCode);
    await requireOpenDraft(storageRoot, artifactCode, draftId);
    const dir = draftDir(storageRoot, artifactCode, draftId);

    // `-f` matters: without it a .gitignore inside the draft silently drops a file the
    // agent put on the desk and was told was saved. `-A` re-syncs the index to the
    // worktree in both directions, which is also what keeps an entry staged by a crashed
    // batch — one with no file behind it any more — out of this commit.
    await gitWork(dir, ['add', '-A', '-f', '--', '.']);

    let staged = false;
    // `--quiet` documents exit 1 as "differences found". Every OTHER nonzero exit is a
    // failure of the command, and reading it as "there are changes" commits on the
    // strength of an error.
    try { await gitWork(dir, ['diff', '--cached', '--quiet']); }
    catch (err) {
      if (!(err instanceof GitFailure)) throw err;
      if (err.exitCode !== 1) {
        throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the draft could not be read', { cause: err });
      }
      staged = true;
    }

    if (staged) {
      const trailerBlock = `Agento-Job: ${trailers.jobId ?? '-'}\n` +
        `Agento-Agent-View: ${trailers.agentView ?? '-'}\n` +
        `Agento-Draft: ${draftId}`;
      // Mapped here rather than left to the service's boundary net: this call is the one
      // that puts an agent-supplied string in argv, so it is the call whose failure must
      // be named by the layer that knows what it was doing.
      try {
        await gitWork(dir, ['commit', '--quiet', '-m', message || 'update', '-m', trailerBlock]);
      } catch (err) {
        if (!(err instanceof GitFailure)) throw err;
        throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the draft could not be saved', { cause: err });
      }
    }

    // Returned whether or not anything was committed: item 9 asks `versionIdForCommit`
    // about this exact value to tell a retry from a new save.
    const commit = out(await gitWork(dir, ['rev-parse', 'HEAD'], { maxBytes: 4096 })).trim();
    return { committed: staged, commit };
  }

  /** A fresh version id, retried on the ONE failure `createVersionRef` can distinguish
   *  from a real error: an id already in use. */
  async function mintVersion(repo, commit) {
    let lastErr = null;
    for (let i = 0; i < MAX_ID_RETRIES; i += 1) {
      const candidate = newVersionId();
      try {
        await createVersionRef(repo, candidate, commit);
        return candidate;
      } catch (err) {
        if (err instanceof ArtifactError && err.code === ERROR_CODES.VERSION_ALREADY_EXISTS) { lastErr = err; continue; }
        throw err;
      }
    }
    throw lastErr;
  }

  /** Desk → version. Three steps, no completion marker: the draft branch tip already
   *  records everything a retry must know, so the marker the old `finalize` needed has
   *  nothing left to say.
   *
   *  The draft stays OPEN. A second call therefore yields a second version, and
   *  `discard_draft` is the only way to close a draft. */
  async function saveVersion(storageRoot, artifactCode, draftId, deskFd, options = {}) {
    const { description = '', limits = {}, trailers = {} } = options;
    const repo = await requireArtifact(storageRoot, artifactCode);
    await requireOpenDraft(storageRoot, artifactCode, draftId);

    // (a) The desk into the worktree, before anything else mutates. Idempotent by
    //     construction — a retry re-mirrors the same desk. The caller opened the desk
    //     with `create: false`, so a desk that is GONE has already failed by here and
    //     the draft is untouched; a desk the agent EMPTIED reaches this line and its
    //     deletions are saved, which is the distinction the two must not collapse into.
    mirrorOut(deskFd, draftDir(storageRoot, artifactCode, draftId), limits);

    // (b)
    const { committed, commit } = await commitDraft(storageRoot, artifactCode, draftId, description, trailers);

    // (c) Nothing to commit has two readings and the store can tell them apart. An id
    //     for this tip means a completed save is being retried — or a second save of
    //     bytes some version already holds, which from content alone is the same event;
    //     returning that id is the only answer correct for both. `null` is the crash
    //     window: a commit exists with no version ref, so mint the ref for THAT tip
    //     rather than committing the same bytes twice.
    if (!committed) {
      const existing = await versionIdForCommit(repo, commit);
      if (existing) return { version_id: existing, source_draft: draftId, revision: commit.slice(0, 7) };
    }
    return { version_id: await mintVersion(repo, commit), source_draft: draftId, revision: commit.slice(0, 7) };
  }

  async function recoverDraft(storageRoot, artifactCode, draftId) {
    await requireArtifact(storageRoot, artifactCode);
    await requireOpenDraft(storageRoot, artifactCode, draftId);
    const dir = draftDir(storageRoot, artifactCode, draftId);
    // `--ignored`: an ignored stray file is still contamination, and `clean -fdx`
    // is what removes it. Without it a draft whose own .gitignore covers the
    // debris reports itself clean and recovery silently does nothing.
    const dirty = out(await gitWork(dir, ['status', '--porcelain', '-z', '--ignored'], { maxBytes: DEFAULT_MAX_OUTPUT }));
    if (dirty === '') return { recovered: false };
    // NOT swallowed: recovery reporting success while the worktree is still dirty
    // hands the next mutation a contaminated tree, which is the exact state this
    // function exists to prevent.
    // `reset --hard`, not `checkout -- .`: the index is part of the contamination,
    // and checkout restores the worktree FROM the index, so entries a crashed save
    // had already staged would survive into the next revision.
    await gitWork(dir, ['reset', '--hard', '--quiet', 'HEAD']);
    await gitWork(dir, ['clean', '-fdx', '--quiet']);
    return { recovered: true };
  }

  // ------------------------------------------------------------------- diff

  async function diff(storageRoot, artifactCode, draftId, against = 'base', { maxDiffBytes = DEFAULT_MAX_DIFF_BYTES } = {}) {
    const repo = await requireArtifact(storageRoot, artifactCode);
    await requireOpenDraft(storageRoot, artifactCode, draftId);
    let baseCommit;
    if (against === 'base') baseCommit = await readRef(repo, baseRef(draftId));
    else if (against === 'current') baseCommit = await readRef(repo, REF_CURRENT);
    else baseCommit = await readRef(repo, versionRef(validateVersionId(against)));
    if (!baseCommit) throw new ArtifactError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found');
    const head = await readRef(repo, draftBranch(draftId));

    const numstat = out(await gitBare(repo, ['diff', '--numstat', '-z', '--no-renames', baseCommit, head],
      { maxBytes: DEFAULT_MAX_OUTPUT }));
    let insertions = 0;
    let deletions = 0;
    for (const rec of numstat.split('\0').filter(Boolean)) {
      const [adds, dels] = rec.split('\t');
      insertions += Number(adds) || 0;
      deletions += Number(dels) || 0;
    }

    const nameStatus = out(await gitBare(repo, ['diff', '--name-status', '-z', '--no-renames', baseCommit, head],
      { maxBytes: DEFAULT_MAX_OUTPUT })).split('\0').filter(Boolean);
    const STATUS = { A: 'added', M: 'modified', D: 'deleted', T: 'modified' };
    const files = [];
    for (let i = 0; i + 1 < nameStatus.length; i += 2) {
      files.push({ path: nameStatus[i + 1], status: STATUS[nameStatus[i][0]] || 'modified' });
    }

    // truncateAt, not a post-hoc slice: the bytes past the budget are never read
    // into the process at all. Counts come from --numstat, which stays small, so
    // a truncated diff still reports accurate totals.
    const text = await gitBare(repo, ['diff', baseCommit, head], { truncateAt: maxDiffBytes });
    return {
      files_changed: files.length, insertions, deletions, files,
      diff: text.stdout.toString('utf8'), truncated: text.truncated,
    };
  }

  async function discardDraft(storageRoot, artifactCode, draftId) {
    const repo = await requireArtifact(storageRoot, artifactCode);
    validateDraftId(draftId);
    const state = await draftState(storageRoot, artifactCode, draftId);
    if (state === 'missing') throw new ArtifactError(ERROR_CODES.DRAFT_NOT_FOUND, 'draft not found');
    if (state !== 'discarding') {
      const head = await readRef(repo, draftBranch(draftId));
      // The marker makes a partial discard resumable rather than stranding a branch.
      // Losing it silently removes that safety net for exactly the crash it covers,
      // so a store that cannot record it fails the discard instead.
      if (head) {
        try { await gitBare(repo, ['update-ref', discardingRef(draftId), head, '']); }
        catch (err) {
          if (!(err instanceof GitFailure)) throw err;
          if (!(await refExists(repo, discardingRef(draftId)))) {
            throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the draft could not be discarded', { cause: err });
          }
        }
      }
    }
    await teardownDraft(storageRoot, artifactCode, draftId);
    await deleteRefVerified(repo, discardingRef(draftId), 'the draft could not be discarded');
    return { draft_id: draftId, discarded: true };
  }

  /** The whole artifact directory — the bare repo, every open worktree, the locks and
   *  the `owner` marker with them. Reports whether it was there, for the same reason the
   *  published-tree twin does. No Git step: a bare repo is a directory, and asking Git
   *  to unmake one only adds a way for the removal to half-succeed. */
  async function removeArtifact(storageRoot, artifactCode) {
    const root = artifactRoot(storageRoot, artifactCode);
    if (!(await exists(root))) return false;
    await rm(root, { recursive: true, force: true });
    return true;
  }

  return {
    init, getCurrent, listVersions, publish, materialize, materializePublished, sweepScratch,
    createDraft, commitDraft, saveVersion, diff, discardDraft,
    recoverDraft, draftState, reconcileDrafts, listOpenDrafts, listArtifacts, getDraftPath,
    readOwningView, removeArtifact,
  };
}
