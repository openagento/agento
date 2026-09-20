import path from 'node:path';
import { ArtifactError, GitFailure, ERROR_CODES, errorFacts } from './errors.js';
import { validateArtifactCode, validateDraftId, validateVersionId,
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
// reads 0 as "keep every preview", so `asPosInt` cannot validate it.
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
  remove: 'versioned_artifact.artifact.removed',
};

/** How many names to try before giving up. The per-caller creation quota
 *  (`limits/max_agent_artifacts`) bounds how many an agent can hold at all, so this only
 *  has to exceed the largest sensible quota — it is a stop, not a policy. */
const MAX_NAME_ATTEMPTS = 200;

/** `name` → `name-2`, `name-3`, … The suffix is APPENDED, never spliced over a number
 *  the caller's own name ends with: stripping a trailing `-\d+` first would turn a
 *  taken `plan-2024` into `plan-2` and silently discard the year. A caller who really
 *  did ask for `report-2` and lost it gets `report-2-2`, which is ugly and truthful.
 *  The result is validated by `be.init`, which refuses a code the regex rejects — a base
 *  long enough that the suffix pushes it past 64 characters fails loudly rather than
 *  being truncated into a different artifact. */
const suffixed = (base, n) => `${base}-${n}`;

/** Did this creation fail because the NAME is taken, as opposed to the store failing?
 *  Matched on the backend's own sentinel rather than on message text. */
const isNameTaken = (err) => err instanceof ArtifactError
  && err.code === ERROR_CODES.ARTIFACT_ALREADY_EXISTS;

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

  /** The agent_view this session acts as, as the store spells it. `null` means "owns
   *  nothing" — a caller that sends `?agent_view_id=abc` parses to NaN and one that
   *  sends `0` parses to 0, and neither may own an artifact.
   *
   *  It SCOPES, it does not authorize: `agent_view_id` is asserted by the caller on the
   *  SSE URL, so a forged one reaches another view's artifacts. See ROADMAP.md — the fix
   *  is session-bound identity in the framework, not a check here.
   *
   *  This replaced an `av<id>-` code PREFIX. The prefix carried ownership in the NAME,
   *  which meant the agent had to know and type it, and every artifact wore an operator
   *  concern in its URL. Ownership now lives in the artifact, where the store is already
   *  the authority on what exists, and the name is free to be a name. */
  const ownView = Number.isSafeInteger(agentViewId) && agentViewId > 0 ? String(agentViewId) : null;

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
  const mayUse = async (artifactCode) => {
    if (admin || allowedArtifacts.includes(artifactCode)) return true;
    if (ownView === null) return false;
    return (await be.readOwningView(storageRoot, artifactCode)) === ownView;
  };

  /** The owner read costs one small file per artifact, so a listing pays it per entry.
   *  Sequential on purpose: a store with thousands of artifacts would otherwise open
   *  thousands of descriptors at once, and the listing is not on any hot path. */
  const filterUsable = async (codes) => {
    const usable = [];
    for (const code of codes) if (await mayUse(code)) usable.push(code);
    return usable;
  };

  async function assertArtifactAllowed(artifactCode) {
    validateArtifactCode(artifactCode);
    if (!(await mayUse(artifactCode))) {
      throw new ArtifactError(ERROR_CODES.ARTIFACT_ACCESS_DENIED, `artifact '${artifactCode}' is not available`);
    }
  }

  // A DB that cannot hand out a pool degrades to `null` DELIBERATELY, and this is
  // not the swallow the startup sweep was corrected for: `recordAudit` treats a null
  // pool as a failed INSERT, so the event is logged AND appended to
  // audit-fallback.log. The row survives; nothing reports a success it did not have.
  const pool = () => { try { return db?.getCronPool?.() ?? null; } catch { return null; } };
  // ONE lock per artifact, taken by every mutation AND by `remove`, so a delete never
  // runs beside a draft, save or publish on the same artifact. It lives under `.locks`,
  // OUTSIDE the artifact root that `remove` deletes — so the holder's lock survives the
  // removal — and `.locks` cannot collide with an artifact_code, which must start [a-z0-9].
  const lifecycleLock = (artifactCode) =>
    path.join(storageRoot, '.locks', validateArtifactCode(artifactCode), 'lifecycle.lock');
  // Serializes one owner's creations, so its cap check and its create are atomic. The
  // lifecycle lock is per-name and cannot: two inits of DIFFERENT names would both read
  // the same count and both slip past the cap.
  const ownerLock = () => path.join(storageRoot, '.locks', `owner-${ownView ?? 'anon'}.lock`);

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
      return withLock(lifecycleLock(artifactCode), async () => {
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
       *  An agent creates under any free name and OWNS what it created; an operator
       *  hands one view another's artifact through `allowed_artifacts`, with no new
       *  mechanism. The name asked for is a wish — the answer carries the real one.
       *  `files` is CLI-only — the tool schema declares no such parameter, and an agent
       *  fills version 1 through a draft on its desk, so no agent-supplied tree ever
       *  enters here. */
      async init(artifactCode, { files = [], title = null, owner = null } = {}) {
        // The ONLY check above the audit boundary, and it must stay there: an invalid
        // code audited is up to 64 bytes of arbitrary caller text written into
        // `versioned_artifact_audit` and into audit-fallback.log, on demand.
        validateArtifactCode(artifactCode);
        // Every POLICY refusal below is INSIDE it — a denied creation is an attempt on
        // the store and the row's `error_code` column exists to record exactly that.
        return audited(OPS.init, { artifactCode }, async () => {
          // A name the OPERATOR chose — the admin CLI, or a code pre-granted through
          // `allowed_artifacts`. Such a name is taken literally: it is created exactly as
          // spelled and a collision is an error, because the operator picked that address
          // on purpose (the "pretty URL" path) and quietly publishing at a different one
          // is not a service. Everything else is an agent naming its own work, where the
          // name is a wish and the next free number is a better answer than a refusal.
          const namedByOperator = admin || allowedArtifacts.includes(artifactCode);
          if (!namedByOperator && ownView === null) {
            throw new ArtifactError(ERROR_CODES.ARTIFACT_ACCESS_DENIED, 'this session may not create artifacts');
          }
          // The cap counts what this caller may USE, not the store: a store-wide count
          // is a cross-view cardinality oracle and, with delete operator-only, a one-way
          // lockout. It bounds ownership, NOT disk. Cap check and create run TOGETHER
          // under the owner lock — without it two inits of different names both read the
          // same count and both slip past the cap.
          const createUnderCap = async () => {
            if (!admin) {
              const held = (await filterUsable(await be.listArtifacts(storageRoot))).length;
              if (held >= limits.max_agent_artifacts) {
                throw new ArtifactError(ERROR_CODES.ARTIFACT_LIMIT_REACHED,
                  `this scope already holds ${held} artifacts`);
              }
            }
            // The name the caller ASKED for is a wish, not the identity: a taken name is
            // answered with `-2`, `-3`, … because the caller cannot see what another
            // agent_view already took. The ADMIN path keeps the error: an operator names
            // a code deliberately, and renaming it would publish at an address nobody chose.
            let created = null;
            let finalCode = artifactCode;
            for (let attempt = 1; created === null; attempt += 1) {
              const candidate = attempt === 1 ? artifactCode : suffixed(artifactCode, attempt);
              try {
                // eslint-disable-next-line no-await-in-loop
                created = await withLock(lifecycleLock(candidate), async () => {
                  const made = await be.init(storageRoot, candidate, {
                    files, allowSymlinks, limits, owningView: admin ? null : ownView,
                  });
                  // The row only DECORATES the store, so a failed INSERT costs a title,
                  // never the artifact — the same degradation `recordAudit` has.
                  const db2 = pool();
                  if (db2) {
                    try {
                      await db2.execute(
                        'INSERT INTO versioned_artifact (artifact_code, title, owner) VALUES (?, ?, ?)',
                        [candidate, title, owner]);
                    } catch (err) {
                      log?.('versioned_artifacts', 'ERROR',
                        `artifact metadata not recorded for '${candidate}': ${errorFacts(err) ?? 'unknown'}`);
                    }
                  }
                  return made;
                });
                finalCode = candidate;
              } catch (err) {
                // Racing on `be.init` rather than checking existence first is what makes
                // the wish safe: two sessions asking one name lose to the same check, and
                // the loser takes the next number.
                if (!isNameTaken(err) || namedByOperator || attempt >= MAX_NAME_ATTEMPTS) throw err;
              }
            }
            return { created, finalCode };
          };
          const { created, finalCode } = admin
            ? await createUnderCap()
            : await withLock(ownerLock(), createUnderCap);
          // Everything below names the artifact that EXISTS, never the one that was
          // asked for: they differ whenever the wished-for name was taken.
          // AFTER the lock, the way `save_version` publishes: `init` already answers a
          // preview URL, so version 1 must be on disk and `current` must point at it
          // before anyone follows that URL. Never fatal — the artifact exists in the
          // store either way, and `publish` rebuilds the tree.
          try {
            await publishVersionTree(finalCode, created.current_version, { swap: true });
          } catch (err) {
            log?.('versioned_artifacts', 'ERROR',
              `preview unavailable for '${finalCode}' ${created.current_version}: ${errorFacts(err) ?? 'unknown'}`);
          }
          return { ...created, preview_url: published.previewUrl(publicBaseUrl, finalCode) };
          // The audit row records what was CREATED. On the error path `describe` is not
          // called and the row keeps the requested name, which is the right record of a
          // creation that never happened.
        }, (r) => ({ artifactCode: r.artifact_code, versionId: r.current_version }));
      },

      /** Destruction, and the ONE lifecycle step the agent does not own: it is reachable
       *  from `artifact:delete` only, never from a tool. `agent_view_id` is asserted by
       *  the caller, so ownership scopes rather than authorizes — a self-asserted identity
       *  must not be able to unmake an immutable history.
       *
       *  The published tree goes FIRST: it is the only root anyone can read over HTTP, so
       *  a removal that dies halfway has stopped serving rather than left a live preview
       *  of an artifact the store no longer holds. Existence is checked on BOTH roots, not
       *  through `requireArtifact`, because the half-cleaned store is exactly the state an
       *  operator runs this to repair.
       *
       *  Takes the lifecycle lock — the SAME lock every mutation takes, and one that lives
       *  OUTSIDE the removed root — so a delete never runs beside an in-flight draft, save
       *  or publish on the same artifact. */
      async remove(artifactCode) {
        // Above the audit boundary for the same reason as `init`: an invalid code audited
        // is arbitrary caller text written into the audit table and the fallback log.
        validateArtifactCode(artifactCode);
        return audited(OPS.remove, { artifactCode }, async () => {
          if (!admin) {
            throw new ArtifactError(ERROR_CODES.ARTIFACT_ACCESS_DENIED, 'this session may not delete artifacts');
          }
          return withLock(lifecycleLock(artifactCode), async () => {
            const removedPublished = await published.removeArtifact(publishedRoot, artifactCode);
            const removedStore = await be.removeArtifact(storageRoot, artifactCode);
            if (!removedPublished && !removedStore) {
              throw new ArtifactError(ERROR_CODES.ARTIFACT_NOT_FOUND, 'artifact not found');
            }
            // The row only DECORATES the store, so a failed DELETE costs a stale title,
            // never the removal — the same degradation `init`'s INSERT has.
            const db2 = pool();
            if (db2) {
              try {
                await db2.execute('DELETE FROM versioned_artifact WHERE artifact_code = ?', [artifactCode]);
              } catch (err) {
                log?.('versioned_artifacts', 'ERROR',
                  `artifact metadata not removed for '${artifactCode}': ${errorFacts(err) ?? 'unknown'}`);
              }
            }
            return { artifact_code: artifactCode, removed_store: removedStore, removed_published: removedPublished };
          });
        });
      },

      // ------------------------------------------------------------- reads
      async getCurrent(artifactCode) {
        await assertArtifactAllowed(artifactCode);
        const current = await be.getCurrent(storageRoot, artifactCode);
        return { ...current, preview_url: published.previewUrl(publicBaseUrl, artifactCode) };
      },
      async listVersions(artifactCode, opts = {}) {
        await assertArtifactAllowed(artifactCode);
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
        await assertArtifactAllowed(artifactCode);
        selectorSourceId(selector);   // reject a selector that names two sources or none
        return withLock(lifecycleLock(artifactCode), () =>
          be.materialize(storageRoot, artifactCode, selector, deskFd));
      },
      /** What this scope may use ∩ what the store holds, each with the state an agent
       *  needs to resume: the current version and the drafts still open. The
       *  `versioned_artifact` table only DECORATES that — the store is the authority on
       *  what exists, so a table that cannot be read costs a title, never an artifact. */
      async listArtifacts() {
        const codes = await filterUsable(await be.listArtifacts(storageRoot));
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
        await assertArtifactAllowed(artifactCode);
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
          await assertArtifactAllowed(artifactCode);
          return withLock(lifecycleLock(artifactCode), async () => {
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
          await assertArtifactAllowed(artifactCode);
          const saved = await withLock(lifecycleLock(artifactCode), async () => {
            await prepareDraft(artifactCode, draftId);
            return be.saveVersion(storageRoot, artifactCode, draftId, deskFd,
              { description, limits, trailers: { jobId, agentView: agentViewId } });
          });
          // AFTER the lifecycle lock is released, then re-taken by publishVersionTree —
          // sequential, never nested. The version is immutable, so nothing can change it
          // between the two. A published tree that cannot be written must NEVER fail a
          // save that already succeeded in the store: the work is safe, only the preview.
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
          await assertArtifactAllowed(artifactCode);
          return withLock(lifecycleLock(artifactCode), async () => {
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
          await assertArtifactAllowed(artifactCode);
          return withLock(lifecycleLock(artifactCode), async () => {
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
      async internalDraftPath(artifactCode, draftId) {
        await assertArtifactAllowed(artifactCode);
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
