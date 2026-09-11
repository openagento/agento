import path from 'node:path';
import { VfError, GitFailure, ERROR_CODES, errorFacts } from './errors.js';
import { validateFolderCode, validateDraftId, validateVersionId, folderRoot } from './paths.js';
import { withLock, sweepStaleLocks } from './locking.js';
import { createBackend } from './git-backend.js';
import { recordAudit } from './audit.js';

// A resolved value is not typed: resolveModuleFieldStrict returns an ENV string
// verbatim and a DB override's raw column value; only a config.json default keeps
// its JSON type. "0"/"false" are therefore TRUTHY strings, and a naive check would
// throw on the safe value — refusing to start over a setting that says "off".
const asBool = (v, field) => {
  if (typeof v === 'boolean') return v;
  const t = String(v ?? '').trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(t)) return true;
  if (['0', 'false', 'no', 'off', ''].includes(t)) return false;
  throw new Error(`versioned_folders: ${field} must be a boolean, got ${JSON.stringify(v)}`);
};

// Rejects NaN, 0, negatives, floats and "5MB" — a limit that cannot be parsed must
// stop the module, never degrade into "no limit".
const asPosInt = (v, field) => {
  const n = typeof v === 'number' ? v : Number(String(v ?? '').trim());
  if (!Number.isSafeInteger(n) || n <= 0) {
    throw new Error(`versioned_folders: ${field} must be a positive integer, got ${JSON.stringify(v)}`);
  }
  return n;
};

function asStorageRoot(v) {
  const raw = String(v ?? '').trim();
  if (!raw) throw new Error('versioned_folders: storage_root is required (config path versioned_folders/storage_root)');
  if (!path.isAbsolute(raw)) throw new Error(`versioned_folders: storage_root must be an absolute path, got ${JSON.stringify(v)}`);
  if (raw.split(/[\\/]+/).includes('..')) throw new Error(`versioned_folders: storage_root must not contain a parent segment, got ${JSON.stringify(v)}`);
  return path.normalize(raw);
}

const OPS = {
  init: 'versioned_folder.folder.initialized',
  createDraft: 'versioned_folder.draft.created',
  applyChanges: 'versioned_folder.draft.changed',
  discardDraft: 'versioned_folder.draft.discarded',
  finalize: 'versioned_folder.version.finalized',
  publish: 'versioned_folder.version.published',
};

export function createService({ config = {}, db = null, log = null, jobId = null, agentViewId = null,
  actor = null, backend = null } = {}) {
  const storageRoot = asStorageRoot(config.storage_root);
  const limits = {
    max_file_size: asPosInt(config['limits/max_file_size'], 'max_file_size'),
    max_total_size: asPosInt(config['limits/max_total_size'], 'max_total_size'),
    max_files: asPosInt(config['limits/max_files'], 'max_files'),
    max_diff_bytes: asPosInt(config['limits/max_diff_bytes'], 'max_diff_bytes'),
  };
  const allowSymlinks = asBool(config['security/allow_symlinks'], 'security/allow_symlinks');
  if (allowSymlinks) {
    throw new Error('versioned_folders: security/allow_symlinks=true is not supported in this release');
  }
  const allowedFolders = String(config.allowed_folders ?? '').split(',').map((s) => s.trim()).filter(Boolean);
  const be = backend || createBackend();

  function assertFolderAllowed(folderCode) {
    validateFolderCode(folderCode);
    // An empty or unset allowlist denies everything — fail closed, consistent
    // with the is_enabled gate.
    if (!allowedFolders.includes(folderCode)) {
      throw new VfError(ERROR_CODES.FOLDER_ACCESS_DENIED, `folder '${folderCode}' is not available`);
    }
  }

  // A DB that cannot hand out a pool degrades to `null` DELIBERATELY, and this is
  // not the swallow the startup sweep was corrected for: `recordAudit` treats a null
  // pool as a failed INSERT, so the event is logged AND appended to
  // audit-fallback.log. The row survives; nothing reports a success it did not have.
  const pool = () => { try { return db?.getCronPool?.() ?? null; } catch { return null; } };
  const draftLock = (folderCode, draftId) =>
    path.join(folderRoot(storageRoot, folderCode), 'locks', `${validateDraftId(draftId)}.lock`);
  const folderLock = (folderCode) =>
    path.join(folderRoot(storageRoot, folderCode), 'locks', 'folder.lock');
  // The init lock cannot live inside the folder: init's first act is to assert the
  // folder does NOT exist, and creating the lock parent would make the backend
  // reject it as already present. `.locks` starts with a dot and a folder_code
  // cannot, so the two can never collide.
  const initLock = (folderCode) =>
    path.join(storageRoot, '.locks', `${validateFolderCode(folderCode)}.lock`);

  function build(currentActor) {
    const audit = (operation, row) => recordAudit(pool(), log, storageRoot, {
      operation, jobId, agentViewId, actor: currentActor, ...row,
    });

    // A FALLBACK, and only for mutations: every backend path that can raise a
    // GitFailure is supposed to map it itself, with the context to say what the
    // failure MEANT. This catches whatever a future unmapped `gitWork` call re-throws
    // raw. What it buys is the AUDIT ROW: an unmapped GitFailure has no `.code`, so
    // the one record an operator has of the failure said only "error", not what
    // failed. Mapping in front of the audit call is why it is here and not further
    // out. It does NOT cover the reads — `getCurrent`, `listVersions`, `listFiles`,
    // `readFile` and `diff` write no audit row and so never enter `audited`; for them
    // the backend's own mapping is the boundary, with `toToolError` as the last net.
    // See GitFailure's comment in errors.js for the three boundaries together.
    const asVfError = (err) => (err instanceof GitFailure
      ? new VfError(ERROR_CODES.GIT_OPERATION_FAILED, 'the storage operation failed', { cause: err })
      : err);

    const audited = async (operation, base, fn, describe) => {
      try {
        const result = await fn();
        await audit(operation, { ...base, ...(describe ? describe(result) : {}), result: 'ok' });
        return result;
      } catch (raw) {
        const err = asVfError(raw);
        await audit(operation, { ...base, result: 'error', errorCode: err?.code ?? null });
        throw err;
      }
    };

    /** A half-torn-down draft is resolved BEFORE recovery, not after: recoverDraft
     *  works inside the worktree that teardown has already removed, so asking it
     *  first would break exactly the retry that was designed to converge. */
    async function prepareDraft(folderCode, draftId, { allowMarkers = false } = {}) {
      const state = await be.draftState(storageRoot, folderCode, draftId);
      if (state === 'open') { await be.recoverDraft(storageRoot, folderCode, draftId); return state; }
      if (allowMarkers && (state === 'finalized' || state === 'discarding')) return state;
      throw new VfError(ERROR_CODES.DRAFT_NOT_FOUND, 'draft not found');
    }

    return {
      // ------------------------------------------------------------ admin
      async init(folderCode, { files = [] } = {}) {
        validateFolderCode(folderCode);
        // Exempt from the allowlist (it runs as an administrator, before any entry
        // could exist) but NOT from the lock or the audit row.
        return audited(OPS.init, { folderCode },
          () => withLock(initLock(folderCode),
            () => be.init(storageRoot, folderCode, { files, allowSymlinks, limits })),
          (r) => ({ versionId: r.current_version }));
      },

      // ------------------------------------------------------------- reads
      async getCurrent(folderCode) {
        assertFolderAllowed(folderCode);
        return be.getCurrent(storageRoot, folderCode);
      },
      async listVersions(folderCode, opts = {}) {
        assertFolderAllowed(folderCode);
        return be.listVersions(storageRoot, folderCode, opts);
      },
      async listFiles(folderCode, selector = {}) {
        assertFolderAllowed(folderCode);
        return be.listFiles(storageRoot, folderCode, selector);
      },
      async readFile(folderCode, selector, relPath) {
        assertFolderAllowed(folderCode);
        return be.readFile(storageRoot, folderCode, selector, relPath);
      },
      async diff(folderCode, draftId, against = 'base') {
        assertFolderAllowed(folderCode);
        return be.diff(storageRoot, folderCode, draftId, against, { maxDiffBytes: limits.max_diff_bytes });
      },

      // --------------------------------------------------------- mutations
      //
      // THE AUDIT BOUNDARY IS THE OUTERMOST ONE. A denied folder, a lock that could
      // not be taken and a draft that is not there are attempts on the store, and
      // the row's `result`/`error_code` columns exist to record exactly that — a
      // boundary drawn inside the lock audits successes and backend failures while
      // the refused attempts, which are the security-interesting ones, leave no
      // trace. Only the SYNTACTIC identifier check stays outside it: an id that is
      // not a well-formed folder_code/draft_id names nothing that could be audited,
      // and writing it into a VARCHAR(64) key column would break the row itself.
      async createDraft(folderCode, baseVersion, description) {
        validateFolderCode(folderCode);
        return audited(OPS.createDraft, { folderCode, description }, async () => {
          assertFolderAllowed(folderCode);
          return withLock(folderLock(folderCode), async () => {
            // Opportunistic cleanup of garbage this call did not create: its failure
            // is logged by the layer that owns a logger, and the valid creation
            // proceeds.
            try { await be.reconcileDrafts(storageRoot, folderCode); }
            catch (err) { log?.('versioned_folders', 'ERROR', `reconcile failed for '${folderCode}': ${errorFacts(err) ?? 'unknown'}`); }
            return be.createDraft(storageRoot, folderCode, baseVersion, description);
          });
        }, (r) => ({ draftId: r.draft_id, versionId: r.base_version }));
      },

      async applyChanges(folderCode, draftId, writes = [], deletes = [], message = '') {
        validateFolderCode(folderCode);
        validateDraftId(draftId);
        return audited(OPS.applyChanges, { folderCode, draftId }, async () => {
          assertFolderAllowed(folderCode);
          return withLock(draftLock(folderCode, draftId), async () => {
            await prepareDraft(folderCode, draftId);
            return be.applyChanges(storageRoot, folderCode, draftId, writes, deletes, message,
              { limits, allowSymlinks, trailers: { jobId, agentView: agentViewId } });
          });
        }, (r) => ({ revision: r.revision }));
      },

      async finalize(folderCode, draftId, description) {
        validateFolderCode(folderCode);
        validateDraftId(draftId);
        return audited(OPS.finalize, { folderCode, draftId, description }, async () => {
          assertFolderAllowed(folderCode);
          return withLock(draftLock(folderCode, draftId), async () => {
            await prepareDraft(folderCode, draftId, { allowMarkers: true });
            try {
              return await be.finalize(storageRoot, folderCode, draftId, description);
            } catch (err) {
              // Teardown is resumable by construction, so the failure that is worth
              // one automatic retry is exactly FINALIZE_FAILED: the version and its
              // marker already exist, the retry skips straight to finishing the
              // teardown, and it happens inside the SAME lock so nothing can open
              // the draft in between. Any other error is a real refusal — retrying
              // it would only repeat the refusal.
              if (!(err instanceof VfError) || err.code !== ERROR_CODES.FINALIZE_FAILED) throw err;
              return await be.finalize(storageRoot, folderCode, draftId, description);
            }
          });
        }, (r) => ({ versionId: r.version_id, revision: r.revision }));
      },

      async discardDraft(folderCode, draftId) {
        validateFolderCode(folderCode);
        validateDraftId(draftId);
        return audited(OPS.discardDraft, { folderCode, draftId }, async () => {
          assertFolderAllowed(folderCode);
          return withLock(draftLock(folderCode, draftId), async () => {
            await prepareDraft(folderCode, draftId, { allowMarkers: true });
            return be.discardDraft(storageRoot, folderCode, draftId);
          });
        });
      },

      async publish(folderCode, versionId, expectedCurrentVersion) {
        // Both identifiers are validated with the folder code, ABOVE the audit
        // boundary, for the same reason the folder code is: a syntactically
        // impossible id names nothing auditable, and auditing it first is what
        // would put an unbounded caller string into the row and the fallback file.
        validateFolderCode(folderCode);
        validateVersionId(versionId);
        validateVersionId(expectedCurrentVersion);
        return audited(OPS.publish, { folderCode, versionId }, async () => {
          assertFolderAllowed(folderCode);
          return withLock(folderLock(folderCode),
            () => be.publish(storageRoot, folderCode, versionId, expectedCurrentVersion));
        }, (r) => ({ previousVersion: r.previous_version }));
      },

      // ---------------------------------------------------------- internal
      internalDraftPath(folderCode, draftId) {
        assertFolderAllowed(folderCode);
        return be.getDraftPath(storageRoot, folderCode, draftId);   // PRD §38 — never a tool
      },

      forActor(nextActor) { return build(nextActor == null ? null : String(nextActor)); },

      /** Startup-only, no agent input: stale locks plus incomplete-draft
       *  reclamation across every folder the STORE holds.
       *
       *  Not the allowlist. `allowed_folders` is agent_view-scoped and the toolbox's
       *  startup pass resolves default-scope overrides only, so with the normal empty
       *  default this loop would reclaim nothing at all; and a folder dropped from
       *  every allowlist would keep its orphaned drafts forever. Reclamation is
       *  maintenance on our own store, not a decision about who may reach it. */
      async startupSweep() {
        const locks = (await sweepStaleLocks(storageRoot)).length;
        let drafts = 0;
        // A store that cannot be listed THROWS, and the caller's outer catch reports
        // the sweep as failed. Logging the error here and returning `{drafts: 0}`
        // anyway was worse than silence: the caller then logged an OK line claiming a
        // completed sweep of zero folders, so the operator's last word on the subject
        // contradicted the error above it.
        const folders = await be.listFolders(storageRoot);
        for (const folderCode of folders) {
          // Each folder in its own try/catch so one damaged folder does not stop
          // the others from being reconciled.
          try { drafts += (await be.reconcileDrafts(storageRoot, folderCode)).length; }
          catch (err) { log?.('versioned_folders', 'ERROR', `reconcile failed for '${folderCode}': ${errorFacts(err) ?? 'unknown'}`); }
        }
        return { locks, drafts };
      },
    };
  }

  return build(actor == null ? null : String(actor));
}
