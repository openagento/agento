import { z } from 'zod';
import { createService } from './service.js';
import { DRAFT_ID_RE, ARTIFACT_CODE_RE, VERSION_ID_RE, selectorSourceId } from './paths.js';
import { openDesk, closeDesk, deskPath, requireSession } from './desk-io.js';
import { ArtifactError, ERROR_CODES, toToolError, errorFacts } from './errors.js';

const ok = (payload) => ({ content: [{ type: 'text', text: JSON.stringify(payload) }] });

const artifactArg = {
  artifact_code: z.string().regex(ARTIFACT_CODE_RE).describe('Stable artifact identifier'),
};

// Bounded at the schema, not only in the service: an identifier that is refused
// below the audit boundary is audited with whatever the caller sent, and the
// fallback file records it verbatim.
const draftId = () => z.string().regex(DRAFT_ID_RE);
const versionId = () => z.string().regex(VERSION_ID_RE);

/** A desk holds exactly one thing, so exactly one of the two may be named. Checked HERE
 *  as well as in the backend because the desk is emptied before the backend is reached:
 *  a malformed request must not be able to wipe the agent's copy first. */
// The desk's NAME comes from the selector, so the same rule that decides which source a
// read names decides which desk it writes to.
const selectorId = (args) => selectorSourceId({ draftId: args.draft_id, versionId: args.version_id });

export async function register(server, context) {
  const { app, log, moduleConfigs, isToolEnabled, db, jobId, agentViewId, artifactsDir } = context;

  // The service is constructed ONCE per register(), before the first
  // server.tool(...), and a construction failure registers nothing: a bad
  // storage_root or limit surfaces at boot, in the operator's log, with no tool
  // exposed — never as ten identical runtime errors seen only by the model.
  let service;
  try {
    service = createService({
      config: moduleConfigs?.versioned_artifacts ?? {},
      db, log, jobId, agentViewId,
    });
  } catch (err) {
    log?.('versioned_artifacts', 'ERROR', `configuration rejected: ${errorFacts(err) ?? 'unknown'}`);
    return;
  }

  // `app` is present ONLY in the startup pass (registerModuleRestApis);
  // registerTools sets it to undefined for every MCP session. Same idiom as
  // jira/github/bitbucket api.js. No module-level flag: run-once must not depend
  // on import caching.
  if (app) {
    try {
      const { locks, drafts } = await service.startupSweep();
      // Logged unconditionally: "swept 0" at every boot is how an operator can
      // tell the sweep RAN.
      log?.('versioned_artifacts', 'OK', `swept ${locks} stale lock(s), reclaimed ${drafts} incomplete draft(s)`);
    } catch (err) {
      log?.('versioned_artifacts', 'ERROR', `startup sweep failed: ${errorFacts(err) ?? 'unknown'}`);
    }
  }

  // A missing isToolEnabled is the startup pass, whose server.tool() is a no-op stub.
  const enabled = (name) => !isToolEnabled || isToolEnabled(name);
  const run = async (fn) => {
    try { return ok(await fn()); }
    catch (err) { return ok(toToolError(err, log)); }
  };

  /** The descriptor is opened HERE and closed in a `finally`, and nothing below this
   *  line resolves the desk by name again — the walk `openDesk` performs is the whole
   *  containment argument, and a path re-derived after it would reopen the window it
   *  closed. `create` is explicit at every call site for the same reason it is in
   *  `openDesk`: absence and emptiness are different states. */
  const withDesk = async (artifactCode, id, create, fn) => {
    const fd = openDesk(artifactsDir, artifactCode, id, { create });
    try { return await fn(fd); } finally { closeDesk(fd); }
  };

  /** Store → desk, for both the tool and `create_draft`. Clearing the desk belongs to
   *  the copy itself, under the service's lock and after the source is extracted — doing
   *  it here would destroy the agent's unsaved work for a read that is then refused. */
  const copyToDesk = (artifactCode, selector, id) =>
    withDesk(artifactCode, id, true, async (fd) => {
      await service.materialize(artifactCode, selector, fd);
      return deskPath(artifactsDir, artifactCode, id);
    });

  if (enabled('versioned_artifact_list')) {
    server.tool('versioned_artifact_list',
      'List the artifacts you can work on, with their current version and open drafts',
      {},
      () => run(async () => ({ artifacts: await service.listArtifacts() })));
  }

  if (enabled('versioned_artifact_get_current')) {
    server.tool('versioned_artifact_get_current',
      'Read which version an artifact currently publishes',
      { ...artifactArg },
      (args) => run(async () => {
        const r = await service.getCurrent(args.artifact_code);
        return { artifact_code: args.artifact_code, current_version: r.current_version,
          preview_url: r.preview_url };
      }));
  }

  if (enabled('versioned_artifact_list_versions')) {
    server.tool('versioned_artifact_list_versions',
      "List an artifact's versions",
      { ...artifactArg, limit: z.number().int().positive().max(200).default(50) },
      (args) => run(async () => {
        const versions = await service.listVersions(args.artifact_code, { limit: args.limit });
        const { current_version } = await service.getCurrent(args.artifact_code);
        return { current_version, versions };
      }));
  }

  if (enabled('versioned_artifact_create_draft')) {
    server.tool('versioned_artifact_create_draft',
      'Create an editable draft from a version and copy it into your workspace',
      { ...artifactArg,
        base_version: z.union([z.literal('current'), versionId()]).default('current')
          .describe("'current' or a version_id"),
        description: z.string().max(255).optional() },
      (args) => run(async () => {
        // Before the store is touched: a session that can hold no desk would otherwise
        // be left with an open draft the agent has no way to reach or close.
        requireSession(artifactsDir);
        const d = await service.createDraft(args.artifact_code, args.base_version, args.description);
        return { draft_id: d.draft_id, base_version: d.base_version,
          path: await copyToDesk(args.artifact_code, { draftId: d.draft_id }, d.draft_id) };
      }));
  }

  if (enabled('versioned_artifact_materialize')) {
    server.tool('versioned_artifact_materialize',
      'Copy a draft or a version into your workspace, replacing whatever is there',
      { ...artifactArg, draft_id: draftId().optional(), version_id: versionId().optional() },
      (args) => run(async () => ({
        path: await copyToDesk(args.artifact_code,
          { draftId: args.draft_id, versionId: args.version_id }, selectorId(args)),
      })));
  }

  if (enabled('versioned_artifact_save_version')) {
    server.tool('versioned_artifact_save_version',
      'Save your workspace copy of a draft as a new immutable version; the draft stays open',
      { ...artifactArg, draft_id: draftId(), description: z.string().max(255) },
      (args) => run(() =>
        // `create: false`: a desk that is GONE must fail, because creating it here would
        // read as "the agent deleted every file" and save an empty version over a good one.
        withDesk(args.artifact_code, args.draft_id, false,
          (fd) => service.saveVersion(args.artifact_code, args.draft_id, fd, args.description))));
  }

  if (enabled('versioned_artifact_diff')) {
    server.tool('versioned_artifact_diff',
      "Compare a draft's SAVED content against its base, current, or a version",
      { ...artifactArg, draft_id: draftId(),
        against: z.union([z.literal('base'), z.literal('current'), versionId()]).default('base')
          .describe("'base', 'current', or a version_id") },
      (args) => run(() => service.diff(args.artifact_code, args.draft_id, args.against)),
      { resultStrategy: 'auto' });
  }

  if (enabled('versioned_artifact_publish')) {
    server.tool('versioned_artifact_publish',
      "Point an artifact's current at an existing version",
      { ...artifactArg, version_id: versionId(),
        // REQUIRED, never defaulted: a default would silently disable the
        // optimistic-concurrency guard of PRD §20.
        expected_current_version: versionId()
          .describe('The version you believe is current; publish fails if it changed') },
      (args) => run(() => service.publish(args.artifact_code, args.version_id, args.expected_current_version)));
  }

  if (enabled('versioned_artifact_discard_draft')) {
    server.tool('versioned_artifact_discard_draft',
      'Delete a draft without creating a version',
      { ...artifactArg, draft_id: draftId() },
      (args) => run(() => service.discardDraft(args.artifact_code, args.draft_id)));
  }
}
