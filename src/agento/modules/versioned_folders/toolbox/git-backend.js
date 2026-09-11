import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { mkdir, rm, stat, writeFile, unlink } from 'node:fs/promises';
import { runGit } from './git-exec.js';
import { VfError, GitFailure, ERROR_CODES } from './errors.js';
import {
  validateFolderCode, validateDraftId, validateVersionId, FOLDER_CODE_RE,
  safeResolve, listDirOrEmpty, folderRoot, repoDir, draftDir, validateRelPath,
} from './paths.js';

// The hardening flags every invocation carries. `core.hooksPath=/dev/null` is
// PRD §32's "hooks disabled"; `core.symlinks=false` makes a symlink in a tree
// materialize as a plain file rather than a link, so a crafted tree cannot
// create one on checkout.
const HARDEN = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.symlinks=false'];

// TWO helpers, and the split is not stylistic:
//   $ git --git-dir <bare> -C <worktree> status
//   fatal: this operation must be run in a work tree
// An explicit `--git-dir` at a BARE repository tells Git there is no work tree,
// and that verdict wins over `-C`. The worktree does not need `--git-dir`: the
// `.git` file `worktree add` writes points back at the repository.
function gitBare(gitDir, args, opts = {}) {
  return runGit([...HARDEN, '--git-dir', gitDir, ...args], opts);   // throws GitFailure, NOT VfError
}
function gitWork(worktreeDir, args, opts = {}) {
  return runGit([...HARDEN, '-C', worktreeDir, ...args], opts);     // same contract
}

const REF_CURRENT = 'refs/agento/current';
const versionRef = (id) => `refs/agento/versions/${id}`;
const draftBranch = (id) => `refs/heads/agento-drafts/${id}`;
const baseRef = (id) => `refs/agento/draft-bases/${id}`;
const finalizedRef = (id) => `refs/agento/finalized/${id}`;
const discardingRef = (id) => `refs/agento/discarding/${id}`;

const MAX_ID_RETRIES = 5;
const DEFAULT_MAX_OUTPUT = 64 * 1024 * 1024;
const DEFAULT_MAX_DIFF_BYTES = 256 * 1024;

const out = (r) => r.stdout.toString('utf8');
/** Absence is ENOENT and nothing else. EACCES, ENOTDIR, EIO and EMFILE all say
 *  "the answer is unknown", and answering them with `false` is what lets a
 *  damaged or unreadable store be reported as FOLDER_NOT_FOUND, DRAFT_NOT_FOUND
 *  or a completed teardown — the same defect class as trusting `rev-parse`'s
 *  exit code below, one layer down. */
const exists = async (p) => {
  try { await stat(p); return true; }
  catch (err) {
    if (err?.code === 'ENOENT') return false;
    throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the folder store could not be read', { cause: err });
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
    throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the folder store could not be read', { cause: err });
  }
  if (out(probe).trim() !== '' || probe.stderr.trim() !== '') {
    throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the folder store is damaged', { cause });
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

  async function requireFolder(storageRoot, folderCode) {
    const root = folderRoot(storageRoot, folderCode);
    if (!(await exists(root))) throw new VfError(ERROR_CODES.FOLDER_NOT_FOUND, 'folder not found');
    return repoDir(storageRoot, folderCode);
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
        throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, message, { cause: err });
      }
    }
  }

  /** Idempotent teardown, shared by finalize, discardDraft and createDraft's
   *  compensating cleanup. Every step is written to succeed on an already-done
   *  store, so a retry converges instead of needing a human. */
  async function teardownDraft(storageRoot, folderCode, draftId) {
    const repo = repoDir(storageRoot, folderCode);
    const dir = draftDir(storageRoot, folderCode, draftId);
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
      throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the draft could not be closed');
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

  async function draftState(storageRoot, folderCode, draftId) {
    const repo = await requireFolder(storageRoot, folderCode);
    validateDraftId(draftId);
    if (await refExists(repo, finalizedRef(draftId))) return 'finalized';
    if (await refExists(repo, discardingRef(draftId))) return 'discarding';
    if (!(await exists(draftDir(storageRoot, folderCode, draftId)))) return 'missing';
    if (!(await refExists(repo, baseRef(draftId)))) return 'incomplete';
    return 'open';
  }

  async function requireOpenDraft(storageRoot, folderCode, draftId) {
    const state = await draftState(storageRoot, folderCode, draftId);
    if (state !== 'open') throw new VfError(ERROR_CODES.DRAFT_NOT_FOUND, 'draft not found');
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
    throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the version pointer is ambiguous');
  }

  /** Create-only, and it VERIFIES before classifying: a non-zero exit alone does
   *  not prove a collision — disk-full and EACCES exit non-zero too. */
  async function createVersionRef(repo, versionId, commit) {
    try {
      await gitBare(repo, ['update-ref', versionRef(versionId), commit, '']);
    } catch (err) {
      if (await refExists(repo, versionRef(versionId))) {
        throw new VfError(ERROR_CODES.VERSION_ALREADY_EXISTS, 'version id already in use');
      }
      // The backend has no logger and must not acquire one — it is a pure adapter.
      // The GitFailure rides along as `cause`; the service logs it once.
      throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'could not record the version', { cause: err });
    }
  }

  /** EXACTLY ONE source. Both tools take `draft_id` and `version_id` as independently
   *  optional arguments, so a caller can name none or both; the contract names one.
   *  Naming both used to select the draft and IGNORE the version silently — the worst
   *  of the three outcomes, because the caller is told nothing and reads a file it did
   *  not ask for. Named nothing is refused here too, and refused BEFORE the store is
   *  touched: a malformed request must not depend on a folder existing. */
  const requireOneSource = (selector) => {
    const hasDraft = selector?.draftId != null;
    const hasVersion = selector?.versionId != null;
    if (hasDraft && hasVersion) {
      throw new VfError(ERROR_CODES.INVALID_PATH, 'name either a draft or a version, not both');
    }
    if (!hasDraft && !hasVersion) {
      throw new VfError(ERROR_CODES.INVALID_PATH, 'a draft or a version must be named');
    }
  };

  const treeishFor = (selector, storageRoot, folderCode) => {
    requireOneSource(selector);
    if (selector?.draftId) return draftBranch(validateDraftId(selector.draftId));
    return versionRef(validateVersionId(selector.versionId));
  };

  async function resolveSelector(storageRoot, folderCode, selector) {
    requireOneSource(selector);
    const repo = await requireFolder(storageRoot, folderCode);
    if (selector?.draftId) {
      await requireOpenDraft(storageRoot, folderCode, selector.draftId);
    } else if (selector?.versionId) {
      if (!(await refExists(repo, versionRef(validateVersionId(selector.versionId))))) {
        throw new VfError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found');
      }
    }
    return { repo, treeish: treeishFor(selector, storageRoot, folderCode) };
  }

  async function treeFiles(repo, treeish) {
    const r = await gitBare(repo, ['ls-tree', '-r', '-l', '-z', treeish], { maxBytes: DEFAULT_MAX_OUTPUT });
    return out(r).split('\0').filter(Boolean).map((rec) => {
      const tab = rec.indexOf('\t');
      const meta = rec.slice(0, tab).split(/\s+/);
      return { path: rec.slice(tab + 1), size: Number(meta[3]) || 0 };
    });
  }

  // ------------------------------------------------------------------- init

  async function init(storageRoot, folderCode, { files = [], allowSymlinks = false, limits = {} } = {}) {
    validateFolderCode(folderCode);
    const root = folderRoot(storageRoot, folderCode);
    if (await exists(root)) throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'folder already exists');

    // Validate the WHOLE payload before creating anything: a half-created folder
    // would make every retry report "already exists".
    const entries = [];
    let total = 0;
    for (const f of files) {
      if (f?.symlink) throw new VfError(ERROR_CODES.SYMLINK_NOT_ALLOWED, 'symbolic links are not allowed in this folder');
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
        throw new VfError(ERROR_CODES.FILE_TOO_LARGE, 'file exceeds the size limit');
      }
      total += buf.length;
      entries.push({ path: rel, buf });
    }
    if (limits.max_files != null && entries.length > limits.max_files) {
      throw new VfError(ERROR_CODES.TOO_MANY_FILES, 'too many files');
    }
    if (limits.max_total_size != null && total > limits.max_total_size) {
      throw new VfError(ERROR_CODES.FOLDER_TOO_LARGE, 'folder exceeds the total size limit');
    }

    const repo = repoDir(storageRoot, folderCode);
    try {
      await mkdir(root, { recursive: true });
      await runGit(['init', '--bare', '--quiet', repo]);

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
      // A temporary file inside the folder being created, already read; a leftover
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
          if (err instanceof VfError && err.code === ERROR_CODES.VERSION_ALREADY_EXISTS) continue;
          throw err;
        }
      }
      if (!versionId) throw lastErr;
      await gitBare(repo, ['update-ref', REF_CURRENT, commit, '']);
      return { folder_code: folderCode, current_version: versionId };
    } catch (err) {
      await rm(root, { recursive: true, force: true });
      throw err;
    }
  }

  // ------------------------------------------------------- current, versions

  async function getCurrent(storageRoot, folderCode) {
    const repo = await requireFolder(storageRoot, folderCode);
    const commit = await readRef(repo, REF_CURRENT);
    if (!commit) throw new VfError(ERROR_CODES.VERSION_NOT_FOUND, 'this folder has no published version');
    const versionId = await versionIdForCommit(repo, commit);
    if (!versionId) throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the current pointer does not name a version');
    return { folder_code: folderCode, current_version: versionId };
  }

  async function listVersions(storageRoot, folderCode, { limit = 50 } = {}) {
    const repo = await requireFolder(storageRoot, folderCode);
    // `--count` is passed to GIT, not applied to the result: slicing afterwards
    // still reads and formats every ref, and a folder gains one version per
    // publish forever. `%(refname:lstrip=3)` — NOT `%(refname:short)`, which for
    // a private namespace strips only `refs/` and would leak `agento/versions/…`
    // into the one field the agent round-trips back into publish.
    // Sorted by the version id, NOT by `creatordate`. These are lightweight refs, so
    // creatordate is the TARGET COMMIT's date — the commit `applyChanges` made, which
    // can predate finalization by any amount. Finalize an old draft after a newer one
    // and "newest first" listed it second, so `limit: 1` omitted the newest version.
    // The id embeds its own UTC finalization timestamp in a fixed-width form, so
    // descending refname IS descending finalization time. Ids minted in the same
    // second still tie — documented, and the random suffix breaks it arbitrarily.
    const r = await gitBare(repo, ['for-each-ref', `--count=${limit}`, '--sort=-refname',
      '--format=%(refname:lstrip=3)%00%(objectname)',
      'refs/agento/versions/'], { maxBytes: DEFAULT_MAX_OUTPUT });
    return out(r).split('\n').filter(Boolean).map((line) => {
      const [version_id, revision] = line.split('\0');
      return { version_id, revision };
    });
  }

  async function publish(storageRoot, folderCode, versionId, expectedCurrentVersion) {
    const repo = await requireFolder(storageRoot, folderCode);
    validateVersionId(versionId);
    validateVersionId(expectedCurrentVersion);
    const targetCommit = await readRef(repo, versionRef(versionId));
    if (!targetCommit) throw new VfError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found');
    const expectedCommit = await readRef(repo, versionRef(expectedCurrentVersion));
    if (!expectedCommit) throw new VfError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found');

    try {
      // The three-argument form fails if the ref no longer equals <old> — the
      // atomic CAS PRD §19 requires.
      await gitBare(repo, ['update-ref', REF_CURRENT, targetCommit, expectedCommit]);
    } catch (err) {
      const actual = await readRef(repo, REF_CURRENT);
      if (actual !== expectedCommit) {
        throw new VfError(ERROR_CODES.CURRENT_VERSION_CHANGED,
          'the published version changed; re-read the current version and decide again');
      }
      throw new VfError(ERROR_CODES.PUBLISH_FAILED, 'could not publish the version', { cause: err });
    }
    return { previous_version: expectedCurrentVersion, current_version: versionId };
  }

  // ----------------------------------------------------------------- drafts

  async function createDraft(storageRoot, folderCode, baseVersion, description) {
    const repo = await requireFolder(storageRoot, folderCode);
    let baseVersionId;
    let baseCommit;
    if (!baseVersion || baseVersion === 'current') {
      ({ current_version: baseVersionId } = await getCurrent(storageRoot, folderCode));
      baseCommit = await readRef(repo, versionRef(baseVersionId));
    } else {
      baseVersionId = validateVersionId(baseVersion);
      baseCommit = await readRef(repo, versionRef(baseVersionId));
    }
    if (!baseCommit) throw new VfError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found');

    const draftId = `d-${randomBytes(3).toString('hex')}`;
    const dir = draftDir(storageRoot, folderCode, draftId);
    await mkdir(path.dirname(dir), { recursive: true });
    await gitBare(repo, ['worktree', 'add', '--quiet', '-b', `agento-drafts/${draftId}`, dir, baseCommit]);
    try {
      await H.afterWorktree(draftId);
      await gitBare(repo, ['update-ref', baseRef(draftId), baseCommit, '']);
    } catch (err) {
      // Compensating cleanup through the SAME idempotent teardown, so a cleanup
      // that itself half-fails leaves a draft the next call can finish removing.
      try { await teardownDraft(storageRoot, folderCode, draftId); } catch { /* reclaimed at startup */ }
      throw err;
    }
    void description;
    return { draft_id: draftId, folder_code: folderCode, base_version: baseVersionId };
  }

  async function reconcileDrafts(storageRoot, folderCode) {
    const root = folderRoot(storageRoot, folderCode);
    const reclaimed = [];
    for (const e of await listDirOrEmpty(path.join(root, 'worktrees'))) {
      if (!e.isDirectory()) continue;
      try { validateDraftId(e.name); } catch { continue; }
      if (await draftState(storageRoot, folderCode, e.name) !== 'incomplete') continue;
      await teardownDraft(storageRoot, folderCode, e.name);
      reclaimed.push(e.name);
    }
    return reclaimed;
  }

  /** Every folder the store actually holds. Deliberately NOT the allowlist: the
   *  allowlist answers "may this caller reach that folder", and startup reclamation
   *  is internal maintenance with no caller — a folder that sits in no agent_view's
   *  allowlist is exactly where an orphan would otherwise stay forever. A directory
   *  that is not a well-formed folder_code, or holds no store, is not a folder. */
  async function listFolders(storageRoot) {
    const folders = [];
    for (const e of await listDirOrEmpty(storageRoot)) {
      if (!e.isDirectory() || !FOLDER_CODE_RE.test(e.name)) continue;
      if (!(await exists(repoDir(storageRoot, e.name)))) continue;
      folders.push(e.name);
    }
    return folders.sort();
  }

  const getDraftPath = (storageRoot, folderCode, draftId) => draftDir(storageRoot, folderCode, draftId);

  // ------------------------------------------------------------------ reads

  async function listFiles(storageRoot, folderCode, selector = {}) {
    // A read path is an LLM-supplied path and gets the SAME validation a write does,
    // BEFORE any store access: Git cannot be traversed out of a tree, so this is not
    // a containment hole — but answering `../escape` with an empty listing tells the
    // caller the folder is empty, where INVALID_PATH tells them what they did wrong.
    const prefix = selector.path == null ? null : validateRelPath(selector.path);
    const { repo, treeish } = await resolveSelector(storageRoot, folderCode, selector);
    let files = await treeFiles(repo, treeish);
    if (prefix) {
      files = files.filter((f) => f.path === prefix || f.path.startsWith(`${prefix}/`));
    }
    if (selector.recursive === false) {
      const base = prefix ? `${prefix}/` : '';
      files = files.filter((f) => !f.path.slice(base.length).includes('/'));
    }
    return files;
  }

  async function readFile(storageRoot, folderCode, selector, relPath) {
    const rel = validateRelPath(relPath);
    const { repo, treeish } = await resolveSelector(storageRoot, folderCode, selector);
    let blob;
    try {
      blob = await gitBare(repo, ['cat-file', 'blob', `${treeish}:${rel}`], { maxBytes: DEFAULT_MAX_OUTPUT });
    } catch (err) {
      if (!(err instanceof GitFailure)) throw err;
      // A read can fail because the file is not there OR because the store cannot be
      // read — an oversized blob, a missing object, a permission error. `cat-file -e`
      // asks the one question that separates them, so a damaged store is not reported
      // to the caller as a file they never wrote.
      // The question is whether the TREE names this path, not whether its object is
      // readable: a blob that is missing from the store is damage, and `cat-file -e`
      // answers "no" to both. `ls-tree` separates them — and a probe that itself
      // fails proves nothing, so it may not be read as absence either.
      let listing;
      try {
        listing = await gitBare(repo, ['ls-tree', '--name-only', '-z', treeish, '--', rel], { maxBytes: 4096 });
      } catch (probeErr) {
        if (!(probeErr instanceof GitFailure)) throw probeErr;
        throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the file could not be read', { cause: err });
      }
      // The RAW buffer, not text: with `-z` a matched path is `name\0` and an
      // unmatched one is zero bytes, so length is the whole test. Decoding and
      // trimming turns the valid filename " " into empty output and reports a
      // damaged blob as a file the caller never wrote.
      if (listing.stdout.length > 0) {
        throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the file could not be read', { cause: err });
      }
      throw new VfError(ERROR_CODES.FILE_NOT_FOUND, 'file not found', { cause: err });
    }
    try {
      const content = new TextDecoder('utf-8', { fatal: true }).decode(blob.stdout);
      return { path: rel, encoding: 'utf-8', content };
    } catch {
      // A NUL-only heuristic would call a Latin-1 file text and hand back
      // replacement characters.
      return { path: rel, encoding: 'binary', content: null };
    }
  }

  // ----------------------------------------------------------------- writes

  /** Compensation, and the ONLY place a Git failure is swallowed unverified. It runs
   *  inside a `catch` that rethrows the original error: turning a failed cleanup into
   *  the thrown error would replace the real cause with its aftermath, and the tree it
   *  failed to clean is cleaned again by `recoverDraft` before the next mutation —
   *  which, unlike this path, refuses to report success without doing it. */
  async function restoreWorktree(dir) {
    // `reset --hard`, not `checkout -- .`: checkout restores the worktree FROM the
    // index, so a batch that had already run `git add` leaves its entries STAGED and
    // the next batch's commit carries files this one was told had failed. `clean`
    // cannot remove them either — by then they are tracked in the index.
    await gitWork(dir, ['reset', '--hard', '--quiet', 'HEAD']).catch(() => {});
    await gitWork(dir, ['clean', '-fdx', '--quiet']).catch(() => {});
  }

  async function applyChanges(storageRoot, folderCode, draftId, writes = [], deletes = [], message = '', options = {}) {
    const { limits = {}, allowSymlinks = false, trailers = {} } = options;
    const repo = await requireFolder(storageRoot, folderCode);
    await requireOpenDraft(storageRoot, folderCode, draftId);
    const dir = draftDir(storageRoot, folderCode, draftId);

    if (writes.length === 0 && deletes.length === 0) {
      throw new VfError(ERROR_CODES.DRAFT_HAS_NO_CHANGES, 'the batch contains no changes');
    }

    // Validate the ENTIRE batch before touching disk.
    const existing = await treeFiles(repo, draftBranch(draftId));
    const byPath = new Map(existing.map((f) => [f.path, f.size]));
    const planned = [];
    for (const w of writes) {
      const rel = validateRelPath(String(w?.path ?? ''));   // canonical: see init
      const abs = await safeResolve(dir, rel, { allowSymlinks });
      const content = String(w?.content ?? '');
      const bytes = Buffer.byteLength(content, 'utf8');   // BYTES, not UTF-16 code units
      if (limits.max_file_size != null && bytes > limits.max_file_size) {
        throw new VfError(ERROR_CODES.FILE_TOO_LARGE, 'file exceeds the size limit');
      }
      planned.push({ rel, abs, content, bytes });
    }
    const removals = [];
    for (const d of deletes) {
      const rel = validateRelPath(String(d ?? ''));         // canonical: see init
      const abs = await safeResolve(dir, rel, { allowSymlinks });
      if (!byPath.has(rel)) throw new VfError(ERROR_CODES.FILE_NOT_FOUND, 'file not found');
      removals.push({ rel, abs });
    }

    // ONE outcome per path. A path written and deleted in the same batch has two
    // contradictory results in the same response — it came back in both `changed`
    // and `deleted` and ended up absent — and a duplicate makes the response
    // describe a final content the caller cannot derive from it. The projected
    // state and the disk operations also apply the two arrays in opposite orders,
    // so the ambiguity was resolved differently by the limit check and by the
    // commit. Refuse the batch instead of picking a winner.
    const written = new Set();
    for (const p of planned) {
      if (written.has(p.rel)) throw new VfError(ERROR_CODES.INVALID_PATH, 'the batch names the same path twice');
      written.add(p.rel);
    }
    const removed = new Set();
    for (const r of removals) {
      if (removed.has(r.rel) || written.has(r.rel)) {
        throw new VfError(ERROR_CODES.INVALID_PATH, 'the batch names the same path twice');
      }
      removed.add(r.rel);
    }

    const projected = new Map(byPath);
    for (const r of removals) projected.delete(r.rel);
    for (const p of planned) projected.set(p.rel, p.bytes);
    if (limits.max_files != null && projected.size > limits.max_files) {
      throw new VfError(ERROR_CODES.TOO_MANY_FILES, 'too many files');
    }
    if (limits.max_total_size != null) {
      let sum = 0;
      for (const size of projected.values()) sum += size;
      if (sum > limits.max_total_size) {
        throw new VfError(ERROR_CODES.FOLDER_TOO_LARGE, 'folder exceeds the total size limit');
      }
    }

    try {
      for (const p of planned) {
        await mkdir(path.dirname(p.abs), { recursive: true });
        await writeFile(p.abs, p.content, 'utf8');
      }
      for (const r of removals) await rm(r.abs, { force: true });

      // `-f` matters: without it a .gitignore inside the draft silently drops a
      // file the agent was told was written.
      await gitWork(dir, ['add', '-A', '-f', '--', '.']);

      let staged = false;
      // `--quiet` documents exit 1 as "differences found". Every OTHER nonzero exit
      // is a failure of the command, and reading it as "there are changes" commits
      // a batch on the strength of an error.
      try { await gitWork(dir, ['diff', '--cached', '--quiet']); }
      catch (err) {
        if (!(err instanceof GitFailure)) throw err;
        if (err.exitCode !== 1) {
          throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the draft could not be read', { cause: err });
        }
        staged = true;
      }
      if (!staged) throw new VfError(ERROR_CODES.DRAFT_HAS_NO_CHANGES, 'the batch changes nothing');

      const trailerBlock = `Agento-Job: ${trailers.jobId ?? '-'}\n` +
        `Agento-Agent-View: ${trailers.agentView ?? '-'}\n` +
        `Agento-Draft: ${draftId}`;
      // Mapped here rather than left to the service's boundary net: this call is the
      // one that puts an agent-supplied string in argv, so it is the call whose
      // failure must be named by the layer that knows what it was doing.
      try {
        await gitWork(dir, ['commit', '--quiet', '-m', message || 'update', '-m', trailerBlock]);
      } catch (err) {
        if (!(err instanceof GitFailure)) throw err;
        throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the batch could not be saved', { cause: err });
      }
      const revision = out(await gitWork(dir, ['rev-parse', '--short', 'HEAD'], { maxBytes: 4096 })).trim();
      return { draft_id: draftId, changed: planned.map((p) => p.rel), deleted: removals.map((r) => r.rel), revision };
    } catch (err) {
      await restoreWorktree(dir);
      throw err;
    }
  }

  async function recoverDraft(storageRoot, folderCode, draftId) {
    await requireFolder(storageRoot, folderCode);
    await requireOpenDraft(storageRoot, folderCode, draftId);
    const dir = draftDir(storageRoot, folderCode, draftId);
    // `--ignored`: an ignored stray file is still contamination, and `clean -fdx`
    // is what removes it. Without it a draft whose own .gitignore covers the
    // debris reports itself clean and recovery silently does nothing.
    const dirty = out(await gitWork(dir, ['status', '--porcelain', '-z', '--ignored'], { maxBytes: DEFAULT_MAX_OUTPUT }));
    if (dirty === '') return { recovered: false };
    // NOT swallowed: recovery reporting success while the worktree is still dirty
    // hands the next mutation a contaminated tree, which is the exact state this
    // function exists to prevent.
    // Same reason as restoreWorktree: the index is part of the contamination, and
    // `checkout -- .` restores from it rather than resetting it.
    await gitWork(dir, ['reset', '--hard', '--quiet', 'HEAD']);
    await gitWork(dir, ['clean', '-fdx', '--quiet']);
    return { recovered: true };
  }

  // ------------------------------------------------------------------- diff

  async function diff(storageRoot, folderCode, draftId, against = 'base', { maxDiffBytes = DEFAULT_MAX_DIFF_BYTES } = {}) {
    const repo = await requireFolder(storageRoot, folderCode);
    await requireOpenDraft(storageRoot, folderCode, draftId);
    let baseCommit;
    if (against === 'base') baseCommit = await readRef(repo, baseRef(draftId));
    else if (against === 'current') baseCommit = await readRef(repo, REF_CURRENT);
    else baseCommit = await readRef(repo, versionRef(validateVersionId(against)));
    if (!baseCommit) throw new VfError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found');
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

  // ---------------------------------------------------------------- finalize

  async function finalize(storageRoot, folderCode, draftId, description) {
    const repo = await requireFolder(storageRoot, folderCode);
    validateDraftId(draftId);
    const state = await draftState(storageRoot, folderCode, draftId);
    let versionId;
    let commit;

    if (state === 'finalized') {
      // Resume: the marker means the version already exists, whatever remains on
      // disk. Skip creation entirely and finish teardown.
      commit = await readRef(repo, finalizedRef(draftId));
      versionId = await versionIdForCommit(repo, commit);
      if (!versionId) throw new VfError(ERROR_CODES.FINALIZE_FAILED, 'the finalized version could not be resolved');
    } else if (state !== 'open') {
      throw new VfError(ERROR_CODES.DRAFT_NOT_FOUND, 'draft not found');
    } else {
      const base = await readRef(repo, baseRef(draftId));
      commit = await readRef(repo, draftBranch(draftId));
      const treeOf = async (c) => out(await gitBare(repo, ['rev-parse', `${c}^{tree}`], { maxBytes: 4096 })).trim();
      if (await treeOf(base) === await treeOf(commit)) {
        throw new VfError(ERROR_CODES.DRAFT_HAS_NO_CHANGES, 'the draft contains no changes to finalize');
      }
      // The version ref AND the completion marker in ONE update-ref --stdin
      // transaction: two sequential writes would leave a publishable version with
      // an open, unmarked draft, and the retry would then mint a SECOND version
      // for identical content.
      let lastErr = null;
      for (let i = 0; i < MAX_ID_RETRIES; i += 1) {
        const candidate = newVersionId();
        const batch = `start\ncreate ${versionRef(candidate)} ${commit}\n` +
          `create ${finalizedRef(draftId)} ${commit}\nprepare\ncommit\n`;
        try {
          await gitBare(repo, ['update-ref', '--stdin'], { input: Buffer.from(batch, 'utf8') });
          versionId = candidate;
          break;
        } catch (err) {
          if (await refExists(repo, versionRef(candidate))) {
            lastErr = new VfError(ERROR_CODES.VERSION_ALREADY_EXISTS, 'version id already in use');
            continue;
          }
          throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'could not record the version', { cause: err });
        }
      }
      if (!versionId) throw lastErr;
    }

    // Teardown AND the marker deletion are one resumable step, so both surface as
    // FINALIZE_FAILED — the only code the service retries. A surviving marker is not
    // cosmetic: it is what makes a repeat `finalize` skip steps 1-3, so reporting
    // success while it stands leaves a ref nothing will ever clean up. Splitting the
    // two boundaries is what previously gave the marker its own GIT_OPERATION_FAILED
    // and silently disabled the retry for the last step of the sequence.
    try {
      await teardownDraft(storageRoot, folderCode, draftId);
      // The version and the marker BOTH survive a failure here: deleting the version
      // would lose the only immutable copy once the worktree or branch is gone. A
      // retry converges, because every completed step is skipped.
      await deleteRefVerified(repo, finalizedRef(draftId), 'the draft could not be closed');
    } catch (err) {
      if (err instanceof VfError && err.code === ERROR_CODES.FINALIZE_FAILED) throw err;
      throw new VfError(ERROR_CODES.FINALIZE_FAILED, 'the draft could not be closed', { cause: err });
    }
    void description;
    return { version_id: versionId, source_draft: draftId, revision: commit.slice(0, 7) };
  }

  async function discardDraft(storageRoot, folderCode, draftId) {
    const repo = await requireFolder(storageRoot, folderCode);
    validateDraftId(draftId);
    const state = await draftState(storageRoot, folderCode, draftId);
    if (state === 'missing' || state === 'finalized') {
      throw new VfError(ERROR_CODES.DRAFT_NOT_FOUND, 'draft not found');
    }
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
            throw new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the draft could not be discarded', { cause: err });
          }
        }
      }
    }
    await teardownDraft(storageRoot, folderCode, draftId);
    await deleteRefVerified(repo, discardingRef(draftId), 'the draft could not be discarded');
    return { draft_id: draftId, discarded: true };
  }

  return {
    init, getCurrent, listVersions, publish,
    createDraft, listFiles, readFile, applyChanges, diff, finalize, discardDraft,
    recoverDraft, draftState, reconcileDrafts, listFolders, getDraftPath,
  };
}
