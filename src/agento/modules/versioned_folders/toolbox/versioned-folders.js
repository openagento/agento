import { z } from 'zod';
import { createService } from './service.js';
import { DRAFT_ID_RE, FOLDER_CODE_RE, VERSION_ID_RE } from './paths.js';
import { toToolError, errorFacts } from './errors.js';

const ok = (payload) => ({ content: [{ type: 'text', text: JSON.stringify(payload) }] });

// `user` is bounded for the same reason `description` is: it lands in a
// VARCHAR(255) column and, when the DB is down, verbatim in audit-fallback.log.
// Zod's .email() constrains the SHAPE, not the LENGTH.
const folderArg = {
  folder_code: z.string().regex(FOLDER_CODE_RE).describe('Stable folder identifier'),
  user: z.string().email().max(255),
};

// Bounded at the schema, not only in the service: an identifier that is refused
// below the audit boundary is audited with whatever the caller sent, and the
// fallback file records it verbatim.
const draftId = () => z.string().regex(DRAFT_ID_RE);
const versionId = () => z.string().regex(VERSION_ID_RE);

export async function register(server, context) {
  const { app, log, moduleConfigs, isToolEnabled, db, jobId, agentViewId } = context;

  // The service is constructed ONCE per register(), before the first
  // server.tool(...), and a construction failure registers nothing: a bad
  // storage_root or limit surfaces at boot, in the operator's log, with no tool
  // exposed — never as ten identical runtime errors seen only by the model.
  let service;
  try {
    service = createService({
      config: moduleConfigs?.versioned_folders ?? {},
      db, log, jobId, agentViewId,
    });
  } catch (err) {
    log?.('versioned_folders', 'ERROR', `configuration rejected: ${errorFacts(err) ?? 'unknown'}`);
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
      log?.('versioned_folders', 'OK', `swept ${locks} stale lock(s), reclaimed ${drafts} incomplete draft(s)`);
    } catch (err) {
      log?.('versioned_folders', 'ERROR', `startup sweep failed: ${errorFacts(err) ?? 'unknown'}`);
    }
  }

  // A missing isToolEnabled is the startup pass, whose server.tool() is a no-op stub.
  const enabled = (name) => !isToolEnabled || isToolEnabled(name);
  const run = async (args, fn) => {
    try { return ok(await fn(service.forActor(args.user))); }
    catch (err) { return ok(toToolError(err, log)); }
  };

  if (enabled('versioned_folder_get_current')) {
    server.tool('versioned_folder_get_current',
      'Read which version a folder currently publishes',
      { ...folderArg },
      (args) => run(args, async (s) => {
        const r = await s.getCurrent(args.folder_code);
        return { folder_code: args.folder_code, current_version: r.current_version };
      }));
  }

  if (enabled('versioned_folder_create_draft')) {
    server.tool('versioned_folder_create_draft',
      'Create an editable draft from a version',
      { ...folderArg,
        base_version: z.union([z.literal('current'), versionId()]).default('current')
          .describe("'current' or a version_id"),
        description: z.string().max(255).optional() },
      (args) => run(args, (s) => s.createDraft(args.folder_code, args.base_version, args.description)));
  }

  if (enabled('versioned_folder_list_files')) {
    server.tool('versioned_folder_list_files',
      'List files in a draft or version',
      { ...folderArg,
        draft_id: draftId().optional(), version_id: versionId().optional(),
        path: z.string().optional(), recursive: z.boolean().default(true) },
      (args) => run(args, async (s) => ({
        files: await s.listFiles(args.folder_code,
          { draftId: args.draft_id, versionId: args.version_id, path: args.path, recursive: args.recursive }),
      })));
  }

  if (enabled('versioned_folder_read_file')) {
    server.tool('versioned_folder_read_file',
      'Read one text file from a draft or version',
      { ...folderArg,
        draft_id: draftId().optional(), version_id: versionId().optional(), path: z.string() },
      (args) => run(args, (s) => s.readFile(args.folder_code,
        { draftId: args.draft_id, versionId: args.version_id }, args.path)));
  }

  if (enabled('versioned_folder_apply_changes')) {
    server.tool('versioned_folder_apply_changes',
      'Write and delete files in a draft as one atomic batch; a path may appear only once, in one of the two lists',
      { ...folderArg, draft_id: draftId(),
        writes: z.array(z.object({ path: z.string(), content: z.string() })).default([]),
        deletes: z.array(z.string()).default([]),
        message: z.string().max(255).describe('Short description of this batch') },
      (args) => run(args, (s) => s.applyChanges(args.folder_code, args.draft_id,
        args.writes, args.deletes, args.message)));
  }

  if (enabled('versioned_folder_diff')) {
    server.tool('versioned_folder_diff',
      'Compare a draft against its base, current, or a version',
      { ...folderArg, draft_id: draftId(),
        against: z.union([z.literal('base'), z.literal('current'), versionId()]).default('base')
          .describe("'base', 'current', or a version_id") },
      (args) => run(args, (s) => s.diff(args.folder_code, args.draft_id, args.against)),
      { resultStrategy: 'auto' });
  }

  if (enabled('versioned_folder_finalize')) {
    server.tool('versioned_folder_finalize',
      'Freeze a draft into an immutable version',
      { ...folderArg, draft_id: draftId(), description: z.string().max(255) },
      (args) => run(args, (s) => s.finalize(args.folder_code, args.draft_id, args.description)));
  }

  if (enabled('versioned_folder_publish')) {
    server.tool('versioned_folder_publish',
      "Point a folder's current at an existing version",
      { ...folderArg, version_id: versionId(),
        // REQUIRED, never defaulted: a default would silently disable the
        // optimistic-concurrency guard of PRD §20.
        expected_current_version: versionId()
          .describe('The version you believe is current; publish fails if it changed') },
      (args) => run(args, (s) => s.publish(args.folder_code, args.version_id, args.expected_current_version)));
  }

  if (enabled('versioned_folder_list_versions')) {
    server.tool('versioned_folder_list_versions',
      "List a folder's versions",
      { ...folderArg, limit: z.number().int().positive().max(200).default(50) },
      (args) => run(args, async (s) => {
        const versions = await s.listVersions(args.folder_code, { limit: args.limit });
        const { current_version } = await s.getCurrent(args.folder_code);
        return { current_version, versions };
      }));
  }

  if (enabled('versioned_folder_discard_draft')) {
    server.tool('versioned_folder_discard_draft',
      'Delete a draft without creating a version',
      { ...folderArg, draft_id: draftId() },
      (args) => run(args, (s) => s.discardDraft(args.folder_code, args.draft_id)));
  }
}
