import path from 'node:path';
import { ArtifactError, GitFailure, ERROR_CODES, errorFacts } from './errors.js';
import { validateArtifactCode, validateDraftId, validateVersionId, artifactRoot,
  selectorSourceId } from './paths.js';
import { withLock, sweepStaleLocks } from './locking.js';
import { createBackend } from './git-backend.js';
import { recordAudit } from './audit.js';
import * as published from './published-tree.js';

// A resolved value is not typed: resolveModuleFieldStrict returns an ENV string
// verbatim and a DB override's raw column value; only a config.json default keeps
// its JSON type. "0"/"false" are therefore TRUTHY strings, and a naive check would
// throw on the safe value — refusing to start over a setting that says "off".
const asBool = (v, field) => {
  if (typeof v === 'boolean') return v;
  const t = String(v ?? '').trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(t)) return true;
  if (['0', 'false', 'no', 'off', ''].includes(t)) return false;
  throw new Error(`versioned_artifacts: ${field} must be a boolean, got ${JSON.stringify(v)}`);
};

// Rejects NaN, 0, negatives, floats and "5MB" — a limit that cannot be parsed must
// stop the module, never degrade into "no limit".
const asPosInt = (v, field) => {
  const n = typeof v === 'number' ? v : Number(String(v ?? '').trim());
  if (!Number.isSafeInteger(n) || n <= 0) {
    throw new Error(`versioned_artifacts: ${field} must be a positive integer, got ${JSON.stringify(v)}`);
  }
  return n;
};

// Rejects NaN, negatives, floats and "5MB" but ACCEPTS 0 — `serving/keep_versions`
// defaults to 0, which means "keep every preview", so `asPosInt` cannot validate it.
const asNonNegInt = (v, field) => {
  const n = typeof v === 'number' ? v : Number(String(v ?? '').trim());
  if (!Number.isSafeInteger(n) || n < 0) {
    throw new Error(`versioned_artifacts: ${field} must be a non-negative integer, got ${JSON.stringify(v)}`);
  }
  return n;
};

function asStorageRoot(v, field = 'storage_root') {
  const raw = String(v ?? '').trim();
  if (!raw) throw new Error(`versioned_artifacts: ${field} is required (config path versioned_artifacts/${field})`);
  if (!path.isAbsolute(raw)) throw new Error(`versioned_artifacts: ${field} must be an absolute path, got ${JSON.stringify(v)}`);
  if (raw.split(/[\\/]+/).includes('..')) throw new Error(`versioned_artifacts: ${field} must not contain a parent segment, got ${JSON.stringify(v)}`);
  return path.normalize(raw);
}

const OPS = {
  init: 'versioned_artifact.artifact.initialized',
  createDraft: 'versioned_artifact.draft.created',
  discardDraft: 'versioned_artifact.draft.discarded',
  saveVersion: 'versioned_artifact.version.saved',
  publish: 'versioned_artifact.version.published',
};

export function createService({ config = {}, db = null, log = null, jobId = null, agentViewId = null,
  actor = null, backend = null, admin = false } = {}) {
  const storageRoot = asStorageRoot(config.storage_root);
  const publishedRoot = asStorageRoot(config.published_root, 'published_root');
  const keepVersions = asNonNegInt(config['serving/keep_versions'], 'serving/keep_versions');
  const publicBaseUrl = String(config['serving/public_base_url'] ?? '').trim();
  const limits = {
    max_file_size: asPosInt(config['limits/max_file_size'], 'max_file_size'),
    max_total_size: asPosInt(config['limits/max_total_size'], 'max_total_size'),
    max_files: asPosInt(config['limits/max_files'], 'max_files'),
    max_diff_bytes: asPosInt(config['limits/max_diff_bytes'], 'max_diff_bytes'),
    max_agent_artifacts: asPosInt(config['limits/max_agent_artifacts'], 'max_agent_artifacts'),
  };
  const allowSymlinks = asBool(config['security/allow_symlinks'], 'security/allow_symlinks');
  if (allowSymlinks) {
    throw new Error('versioned_artifacts: security/allow_symlinks=true is not supported in this release');
  }
  const allowedArtifacts = String(config.allowed_artifacts ?? '').split(',').map((s) => s.trim()).filter(Boolean);
  const be = backend || createBackend();

  /** The namespace an agent_view creates and works in. DERIVED, never configured and
   *  never stored: there is no marker file to half-write and no pre-existing artifact
   *  to migrate. The empty string means "owns nothing" — a caller that sends
   *  `?agent_view_id=abc` parses to NaN and one that sends `0` parses to 0, and an empty
   *  prefix would make `startsWith` true for every code in the store.
   *
   *  It SCOPES, it does not authorize: `agent_view_id` is asserted by the caller on the
   *  SSE URL, so a forged one reaches another view's namespace. See ROADMAP.md — the fix
   *  is session-bound identity in the framework, not a check here. */
  const ownNamespace = Number.isSafeInteger(agentViewId) && agentViewId > 0 ? `av${agentViewId}-` : '';

  /** The ONE rule for "may this caller touch that artifact", used by the gate below and
   *  by the listing, which used to spell it out a second time and could drift from it.
   *
   *  `admin` is the SAME exemption `init` has always carried, widened to the other
   *  operations the admin CLI performs: `allowed_artifacts` is agent_view-scoped and
   *  empty by default, while the CLI resolves DEFAULT scope — so without it
   *  `artifact:list` would print nothing and `artifact:publish` would be denied on every
   *  deployment. Set ONLY by `cli.js`, never derived from the `actor` string, and never
   *  reachable from the tool layer.
   *
   *  `allowed_artifacts` is the operator's grant, and it is what hands an artifact from
   *  one agent_view to another — one `config:set`, no new mechanism. It is not a second
   *  allow-list beside `is_enabled`: `is_enabled` decides whether the tool exists at all,
   *  this decides which artifacts it may name. */
  const mayUse = (artifactCode) => admin
    || allowedArtifacts.includes(artifactCode)
    || (ownNamespace !== '' && artifactCode.startsWith(ownNamespace));

  function assertArtifactAllowed(artifactCode) {
    validateArtifactCode(artifactCode);
    if (!mayUse(artifactCode)) {
      throw new ArtifactError(ERROR_CODES.ARTIFACT_ACCESS_DENIED, `artifact '${artifactCode}' is not available`);
    }
  }

  // A DB that cannot hand out a pool degrades to `null` DELIBERATELY, and this is
  // not the swallow the startup sweep was corrected for: `recordAudit` treats a null
  // pool as a failed INSERT, so the event is logged AND appended to
  // audit-fallback.log. The row survives; nothing reports a success it did not have.
  const pool = () => { try { return db?.getCronPool?.() ?? null; } catch { return null; } };
  const lockFile = (artifactCode, name) =>
    path.join(artifactRoot(storageRoot, artifactCode), 'locks', `${name}.lock`);
  const draftLock = (artifactCode, draftId) => lockFile(artifactCode, validateDraftId(draftId));
  const artifactLock = (artifactCode) =>
    path.join(artifactRoot(storageRoot, artifactCode), 'locks', 'artifact.lock');
  // The init lock cannot live inside the artifact: init's first act is to assert the
  // artifact does NOT exist, and creating the lock parent would make the backend
  // reject it as already present. `.locks` starts with a dot and an artifact_code
  // cannot, so the two can never collide.
  const initLock = (artifactCode) =>
    path.join(storageRoot, '.locks', `${validateArtifactCode(artifactCode)}.lock`);

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
    // out. It does NOT cover the reads — `getCurrent`, `listVersions`, `listArtifacts`,
    // `materialize` and `diff` write no audit row and so never enter `audited`; for them
    // the backend's own mapping is the boundary, with `toToolError` as the last net.
    // See GitFailure's comment in errors.js for the three boundaries together.
    const asArtifactError = (err) => (err instanceof GitFailure
      ? new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the storage operation failed', { cause: err })
      : err);

    const audited = async (operation, base, fn, describe) => {
      try {
        const result = await fn();
        await audit(operation, { ...base, ...(describe ? describe(result) : {}), result: 'ok' });
        return result;
      } catch (raw) {
        const err = asArtifactError(raw);
        await audit(operation, { ...base, result: 'error', errorCode: err?.code ?? null });
        throw err;
      }
    };

    /** A half-torn-down draft is resolved BEFORE recovery, not after: recoverDraft
     *  works inside the worktree that teardown has already removed, so asking it
     *  first would break exactly the retry that was designed to converge. */
    async function prepareDraft(artifactCode, draftId, { allowMarkers = false } = {}) {
      const state = await be.draftState(storageRoot, artifactCode, draftId);
      if (state === 'open') { await be.recoverDraft(storageRoot, artifactCode, draftId); return state; }
      if (allowMarkers && state === 'discarding') return state;
      throw new ArtifactError(ERROR_CODES.DRAFT_NOT_FOUND, 'draft not found');
    }

    /** Retention is maintenance, never a reason to fail the operation that triggered it. */
    async function pruneQuietly(artifactCode) {
      try { await published.pruneVersions(publishedRoot, artifactCode, keepVersions); }
      catch (err) {
        log?.('versioned_artifacts', 'ERROR', `preview retention failed for '${artifactCode}': ${errorFacts(err) ?? 'unknown'}`);
      }
    }

    /** Materialize one version into the published tree and apply retention. Returns the
     *  relative preview path. Used by `save_version`; `publish` runs the same two steps
     *  around its CAS and cannot share this. */
    async function publishVersionTree(artifactCode, versionId, { swap = false } = {}) {
      return withLock(artifactLock(artifactCode), async () => {
        await be.materializePublished(storageRoot, artifactCode, versionId, {
          destDir: published.publishedVersionDir(publishedRoot, artifactCode, versionId),
          scratchDir: published.scratchRoot(publishedRoot),
        });
        // `init` swaps because it creates the store's `current`; a save does not,
        // since only a publish moves what the server shows. Under this lock the STORE
        // is the authority on what `current` is: `init` swaps AFTER its own lock
        // releases, so a publish can win the artifact lock in between, and installing
        // version 1 then would drag the served pointer off a version the store has
        // already moved to. That publish did its own swap; leave it standing.
        if (swap) {
          const { current_version: live } = await be.getCurrent(storageRoot, artifactCode);
          if (live === versionId) {
            await published.swapCurrent(publishedRoot, artifactCode, versionId);
          } else {
            log?.('versioned_artifacts', 'WARN',
              `preview pointer for '${artifactCode}' left on '${live}': current moved away from ${versionId} before the initial swap`);
          }
        }
        await pruneQuietly(artifactCode);
        return published.previewPath(publishedRoot, artifactCode, versionId);
      });
    }

    return {
      // --------------------------------------------------------- lifecycle
      /** Creation, for the agent as much as for the administrator: the agent owns the
       *  whole loop — init, draft, version, publish, next draft — and an operator is
       *  needed to bootstrap none of it.
       *
       *  An agent creates inside its own `ownNamespace`, or on a code an operator
       *  pre-granted through `allowed_artifacts` (that is the "pretty URL" path, and the
       *  cross-view handoff, with no new mechanism). `files` is CLI-only — the tool
       *  schema declares no such parameter, and an agent fills version 1 through a draft
       *  on its desk, so no agent-supplied tree ever enters here. */
      async init(artifactCode, { files = [], title = null, owner = null } = {}) {
        // The ONLY check above the audit boundary, and it must stay there: an invalid
        // code audited is up to 64 bytes of arbitrary caller text written into
        // `versioned_artifact_audit` and into audit-fallback.log, on demand.
        validateArtifactCode(artifactCode);
        // Every POLICY refusal below is INSIDE it — a denied creation is an attempt on
        // the store and the row's `error_code` column exists to record exactly that.
        return audited(OPS.init, { artifactCode }, async () => {
          if (!mayUse(artifactCode)) {
            // Naming the prefix is what lets the agent retry without an operator.
            throw new ArtifactError(ERROR_CODES.ARTIFACT_ACCESS_DENIED, ownNamespace
              ? `an artifact you create must start with '${ownNamespace}'`
              : 'this session may not create artifacts');
          }
          const created = await withLock(initLock(artifactCode), async () => {
            // Counted over what this caller may USE, not over the store. A count of the
            // whole store answers "how many artifacts does every other agent_view hold"
            // — and, with no delete on any path, lets one view lock creation out for all
            // of them. It bounds namespaces, NOT disk: versions are unbounded and
            // `serving/keep_versions` prunes nothing at its default of 0.
            // ponytail: the lock is per-code, so N concurrent inits can overshoot by N-1.
            if (!admin) {
              const held = (await be.listArtifacts(storageRoot)).filter(mayUse).length;
              if (held >= limits.max_agent_artifacts) {
                throw new ArtifactError(ERROR_CODES.ARTIFACT_LIMIT_REACHED,
                  `this scope already holds ${held} artifacts`);
              }
            }
            const made = await be.init(storageRoot, artifactCode, { files, allowSymlinks, limits });
            // The store is the authority on what exists and the row only DECORATES
            // it, so a failed INSERT costs a title, never the artifact — the same way
            // `recordAudit` degrades. Failing the init here would leave an artifact
            // that exists in the store and reports "creation failed" on every retry.
            const db2 = pool();
            if (db2) {
              try {
                await db2.execute(
                  'INSERT INTO versioned_artifact (artifact_code, title, owner) VALUES (?, ?, ?)',
                  [artifactCode, title, owner]);
              } catch (err) {
                log?.('versioned_artifacts', 'ERROR',
                  `artifact metadata not recorded for '${artifactCode}': ${errorFacts(err) ?? 'unknown'}`);
              }
            }
            return made;
          });
          // AFTER the lock, the way `save_version` publishes: `init` already answers a
          // preview URL, so version 1 must be on disk and `current` must point at it
          // before anyone follows that URL. Never fatal — the artifact exists in the
          // store either way, and `publish` rebuilds the tree.
          try {
            await publishVersionTree(artifactCode, created.current_version, { swap: true });
          } catch (err) {
            log?.('versioned_artifacts', 'ERROR',
              `preview unavailable for '${artifactCode}' ${created.current_version}: ${errorFacts(err) ?? 'unknown'}`);
          }
          return { ...created, preview_url: published.previewUrl(publicBaseUrl, artifactCode) };
        }, (r) => ({ versionId: r.current_version }));
      },

      // ------------------------------------------------------------- reads
      async getCurrent(artifactCode) {
        assertArtifactAllowed(artifactCode);
        const current = await be.getCurrent(storageRoot, artifactCode);
        return { ...current, preview_url: published.previewUrl(publicBaseUrl, artifactCode) };
      },
      async listVersions(artifactCode, opts = {}) {
        assertArtifactAllowed(artifactCode);
        const rows = await be.listVersions(storageRoot, artifactCode, opts);
        // Relative, and null once retention pruned the directory — the version itself
        // is still materializable, only the browser preview is gone.
        return Promise.all(rows.map(async (r) => ({
          ...r, preview_path: await published.previewPath(publishedRoot, artifactCode, r.version_id),
        })));
      },
      /** A read of the store, but a WRITE of the desk — so unlike a listing it takes the
       *  same lock a save of that draft takes. Without it a save can read the desk this
       *  call is in the middle of replacing and record every file as deleted.
       *  `deskFd` is a DESCRIPTOR the tool layer already walked to; this layer never
       *  learns the desk's path and so cannot re-resolve it. No audit row: nothing in
       *  the store changes. */
      async materialize(artifactCode, selector, deskFd) {
        validateArtifactCode(artifactCode);
        assertArtifactAllowed(artifactCode);
        // Before the lock: its NAME is the id the selector gives, so a selector naming
        // two sources or none must be refused first.
        const id = selectorSourceId(selector);
        return withLock(lockFile(artifactCode, id), () =>
          be.materialize(storageRoot, artifactCode, selector, deskFd));
      },
      /** What this scope may use ∩ what the store holds, each with the state an agent
       *  needs to resume: the current version and the drafts still open. The
       *  `versioned_artifact` table only DECORATES that — the store is the authority on
       *  what exists, so a table that cannot be read costs a title, never an artifact. */
      async listArtifacts() {
        const codes = (await be.listArtifacts(storageRoot)).filter((c) => mayUse(c));
        let meta = new Map();
        const db2 = pool();
        if (db2) {
          try {
            const [rows] = await db2.query(
              'SELECT artifact_code, title, owner, created_at FROM versioned_artifact');
            meta = new Map(rows.map((r) => [r.artifact_code, r]));
          } catch (err) {
            log?.('versioned_artifacts', 'ERROR', `artifact metadata unreadable: ${errorFacts(err) ?? 'unknown'}`);
          }
        }
        const out = [];
        for (const artifact_code of codes) {
          const row = meta.get(artifact_code);
          try {
            out.push({
              artifact_code,
              title: row?.title ?? null,
              owner: row?.owner ?? null,
              created_at: row?.created_at ?? null,
              ...(await be.getCurrent(storageRoot, artifact_code)),
              preview_url: published.previewUrl(publicBaseUrl, artifact_code),
              open_drafts: await be.listOpenDrafts(storageRoot, artifact_code),
            });
          } catch (err) {
            // ONE damaged artifact must not hide every healthy one: this is the tool an
            // agent uses to find out what it can still work on, and an agent that can
            // create artifacts is what makes a half-created one reachable here.
            log?.('versioned_artifacts', 'ERROR',
              `artifact '${artifact_code}' not listable: ${errorFacts(err) ?? 'unknown'}`);
          }
        }
        return out;
      },
      async diff(artifactCode, draftId, against = 'base') {
        assertArtifactAllowed(artifactCode);
        return be.diff(storageRoot, artifactCode, draftId, against, { maxDiffBytes: limits.max_diff_bytes });
      },

      // --------------------------------------------------------- mutations
      //
      // THE AUDIT BOUNDARY IS THE OUTERMOST ONE. A denied artifact, a lock that could
      // not be taken and a draft that is not there are attempts on the store, and
      // the row's `result`/`error_code` columns exist to record exactly that — a
      // boundary drawn inside the lock audits successes and backend failures while
      // the refused attempts, which are the security-interesting ones, leave no
      // trace. Only the SYNTACTIC identifier check stays outside it: an id that is
      // not a well-formed artifact_code/draft_id names nothing that could be audited,
      // and writing it into a VARCHAR(64) key column would break the row itself.
      async createDraft(artifactCode, baseVersion, description) {
        validateArtifactCode(artifactCode);
        return audited(OPS.createDraft, { artifactCode, description }, async () => {
          assertArtifactAllowed(artifactCode);
          return withLock(artifactLock(artifactCode), async () => {
            // Opportunistic cleanup of garbage this call did not create: its failure
            // is logged by the layer that owns a logger, and the valid creation
            // proceeds.
            try { await be.reconcileDrafts(storageRoot, artifactCode); }
            catch (err) { log?.('versioned_artifacts', 'ERROR', `reconcile failed for '${artifactCode}': ${errorFacts(err) ?? 'unknown'}`); }
            return be.createDraft(storageRoot, artifactCode, baseVersion, description);
          });
        }, (r) => ({ draftId: r.draft_id, versionId: r.base_version }));
      },

      /** The one operation that reads agent-owned bytes into the store.
       *
       *  `prepareDraft` WITHOUT `allowMarkers`: a save requires a genuinely open draft,
       *  and there is no marker state left for it to resume. There is no self-retry
       *  either — the old one existed to finish a teardown this operation no longer
       *  performs, and a retry around a commit is how one save becomes two versions.
       *  Idempotency comes from the draft tip on the caller's next attempt instead. */
      async saveVersion(artifactCode, draftId, deskFd, description) {
        validateArtifactCode(artifactCode);
        validateDraftId(draftId);
        return audited(OPS.saveVersion, { artifactCode, draftId, description }, async () => {
          assertArtifactAllowed(artifactCode);
          const saved = await withLock(draftLock(artifactCode, draftId), async () => {
            await prepareDraft(artifactCode, draftId);
            return be.saveVersion(storageRoot, artifactCode, draftId, deskFd,
              { description, limits, trailers: { jobId, agentView: agentViewId } });
          });
          // AFTER the draft lock is released, not inside it: the version already exists,
          // so the draft lock has done its job and nesting the artifact lock under it
          // would be the module's only lock ordering. The version is immutable, so
          // nothing can change it between the two locks.
          //
          // A published tree that cannot be written must NEVER fail a save that already
          // succeeded in the store — the agent's work is safe, only the preview is not.
          let previewPath = null;
          try {
            previewPath = await publishVersionTree(artifactCode, saved.version_id);
          } catch (err) {
            log?.('versioned_artifacts', 'ERROR',
              `preview unavailable for '${artifactCode}' ${saved.version_id}: ${errorFacts(err) ?? 'unknown'}`);
          }
          return { ...saved, preview_path: previewPath };
        }, (r) => ({ versionId: r.version_id, revision: r.revision }));
      },

      async discardDraft(artifactCode, draftId) {
        validateArtifactCode(artifactCode);
        validateDraftId(draftId);
        return audited(OPS.discardDraft, { artifactCode, draftId }, async () => {
          assertArtifactAllowed(artifactCode);
          return withLock(draftLock(artifactCode, draftId), async () => {
            await prepareDraft(artifactCode, draftId, { allowMarkers: true });
            return be.discardDraft(storageRoot, artifactCode, draftId);
          });
        });
      },

      async publish(artifactCode, versionId, expectedCurrentVersion) {
        // Both identifiers are validated with the artifact code, ABOVE the audit
        // boundary, for the same reason the artifact code is: a syntactically
        // impossible id names nothing auditable, and auditing it first is what
        // would put an unbounded caller string into the row and the fallback file.
        validateArtifactCode(artifactCode);
        validateVersionId(versionId);
        validateVersionId(expectedCurrentVersion);
        return audited(OPS.publish, { artifactCode, versionId }, async () => {
          assertArtifactAllowed(artifactCode);
          return withLock(artifactLock(artifactCode), async () => {
            // ORDER MATTERS, and the CAS goes second. The store's `current` ref is
            // authoritative: once it moves, a failure after it leaves the store saying
            // v2 while HTTP serves v1, and a retry with the caller's original
            // `expected_current_version` is then refused by the CAS — the served state
            // stuck with no route forward.
            //
            // 1. Materialize the target, re-materializing it if retention pruned it.
            //    This only writes an immutable directory and is safely repeatable; if
            //    it fails, nothing has changed anywhere and the caller retries with the
            //    arguments it already has.
            await be.materializePublished(storageRoot, artifactCode, versionId, {
              destDir: published.publishedVersionDir(publishedRoot, artifactCode, versionId),
              scratchDir: published.scratchRoot(publishedRoot),
            });
            // 2. The CAS — unchanged, and the only serialization point. There is
            //    deliberately NO "skip it when expected equals the target" shortcut:
            //    the recovery call IS the ordinary call with
            //    `expected_current_version === version_id`, and the three-argument
            //    `update-ref` is then a no-op that VERIFIES — it succeeds precisely when
            //    `current` really is that version. A shortcut would publish the tree
            //    while the store still said something else, which is the divergence
            //    this ordering exists to close.
            const result = await be.publish(storageRoot, artifactCode, versionId, expectedCurrentVersion);
            // 3. The swap. A failure here is reported, never thrown: the store change
            //    succeeded, and a thrown error would invite a retry the CAS must refuse.
            let stale = false;
            try {
              await published.swapCurrent(publishedRoot, artifactCode, versionId);
            } catch (err) {
              stale = true;
              log?.('versioned_artifacts', 'WARN', `the served tree for '${artifactCode}' still shows the previous version; `
                + `re-run publish with expected_current_version=${versionId} to repair it: ${errorFacts(err) ?? 'unknown'}`);
            }
            // 4. Retention, last, so the version just installed is present when the
            //    prune reads `current` to decide what it may not delete.
            await pruneQuietly(artifactCode);
            return {
              ...result,
              preview_url: published.previewUrl(publicBaseUrl, artifactCode),
              ...(stale ? { preview_stale: true } : {}),
            };
          });
        }, (r) => ({ previousVersion: r.previous_version }));
      },

      // ---------------------------------------------------------- internal
      internalDraftPath(artifactCode, draftId) {
        assertArtifactAllowed(artifactCode);
        return be.getDraftPath(storageRoot, artifactCode, draftId);   // PRD §38 — never a tool
      },

      /** Startup-only, no agent input: stale locks plus incomplete-draft
       *  reclamation across every artifact the STORE holds.
       *
       *  Not the allowlist. `allowed_artifacts` is agent_view-scoped and the toolbox's
       *  startup pass resolves default-scope overrides only, so with the normal empty
       *  default this loop would reclaim nothing at all; and an artifact dropped from
       *  every allowlist would keep its orphaned drafts forever. Reclamation is
       *  maintenance on our own store, not a decision about who may reach it. */
      async startupSweep() {
        const locks = (await sweepStaleLocks(storageRoot)).length;
        // A `materialize` killed between its extraction and its `finally` leaves a
        // scratch tree behind; `finally` does not run on SIGKILL. Boot is the one
        // moment at which no extraction can be in flight.
        await be.sweepScratch(storageRoot);
        await published.sweepScratch(publishedRoot);
        let drafts = 0;
        // A store that cannot be listed THROWS, and the caller's outer catch reports
        // the sweep as failed. Logging the error here and returning `{drafts: 0}`
        // anyway was worse than silence: the caller then logged an OK line claiming a
        // completed sweep of zero artifacts, so the operator's last word on the subject
        // contradicted the error above it.
        const artifacts = await be.listArtifacts(storageRoot);
        for (const artifactCode of artifacts) {
          // Each artifact in its own try/catch so one damaged artifact does not stop
          // the others from being reconciled.
          try { drafts += (await be.reconcileDrafts(storageRoot, artifactCode)).length; }
          catch (err) { log?.('versioned_artifacts', 'ERROR', `reconcile failed for '${artifactCode}': ${errorFacts(err) ?? 'unknown'}`); }
        }
        return { locks, drafts };
      },
    };
  }

  return build(actor == null ? null : String(actor));
}
