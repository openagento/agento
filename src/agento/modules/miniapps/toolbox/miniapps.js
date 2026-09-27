import { z } from 'zod';
import { createHash } from 'node:crypto';
import { createService } from '../../versioned_artifacts/toolbox/service.js';
import { ARTIFACT_CODE_RE, VERSION_ID_RE, validateArtifactCode, validateVersionId } from '../../versioned_artifacts/toolbox/paths.js';
import { recordAudit } from '../../versioned_artifacts/toolbox/audit.js';
import { toToolError, errorFacts } from '../../versioned_artifacts/toolbox/errors.js';

// Miniapps (PRD E6 §3–§5). Three states stay apart: SAVED is a versioned_artifacts
// version; REACHABLE is materialized and served only through an authorized path;
// ACTIVATED is a `miniapp_activation` row whose fingerprint equals that version's
// manifest. The manifest is read through versioned_artifacts' service, the only code
// that reaches its storage engine.

export const MANIFEST_FILE = 'miniapp.json';
const MAX_MANIFEST_BYTES = 64 * 1024;
const MAX_ACTIONS = 64;
// The tool-name shape the invoke route accepts.
const ACTION_RE = /^[a-z0-9_]{1,64}$/;
const MANIFEST_KEYS = ['schema', 'title', 'actions'];

export class MiniappError extends Error {
  constructor(code, message) { super(`${code}: ${message}`); this.code = code; this.publicMessage = message; }
}

/** The manifest, strict: unknown keys, a wrong type or a duplicate action is null. */
export function parseManifest(bytes) {
  let m;
  try { m = JSON.parse(Buffer.from(bytes).toString('utf8')); } catch { return null; }
  if (!m || typeof m !== 'object' || Array.isArray(m)) return null;
  const keys = Object.keys(m);
  if (keys.length !== MANIFEST_KEYS.length || !MANIFEST_KEYS.every((k) => keys.includes(k))) return null;
  if (m.schema !== 1) return null;
  if (typeof m.title !== 'string' || m.title.length < 1 || m.title.length > 200) return null;
  if (!Array.isArray(m.actions) || m.actions.length > MAX_ACTIONS) return null;
  if (!m.actions.every((a) => typeof a === 'string' && ACTION_RE.test(a))) return null;
  if (new Set(m.actions).size !== m.actions.length) return null;
  return { title: m.title, actions: [...m.actions] };
}

export const fingerprint = (bytes) => createHash('sha256').update(bytes).digest('hex');

const jsonList = (v) => (Array.isArray(v) ? v : JSON.parse(String(v)));

export function createMiniapps({ vaConfig = {}, db = null, log = null, agentViewId = null, actor = null,
  admin = false, service = null } = {}) {
  const va = service ?? createService({ config: vaConfig, db, log, agentViewId, actor, admin });
  const pool = () => db?.getCronPool?.() ?? null;
  const requirePool = () => {
    const p = pool();
    if (!p) throw new MiniappError('MINIAPP_STORE_UNAVAILABLE', 'the activation store is unavailable');
    return p;
  };

  /** The version's manifest and its fingerprint, or null when it has none or it is invalid. */
  async function manifestOf(code, versionId) {
    const bytes = await va.readVersionFile(code, versionId, MANIFEST_FILE, MAX_MANIFEST_BYTES);
    const manifest = bytes && parseManifest(bytes);
    return manifest ? { ...manifest, fingerprint: fingerprint(bytes) } : null;
  }

  async function activationOf(code, versionId) {
    const [rows] = await requirePool().query(
      'SELECT manifest_fingerprint, allowed_actions FROM miniapp_activation WHERE artifact_code = ? AND version_id = ?',
      [code, versionId]);
    return rows?.[0] ?? null;
  }

  const audit = (operation, code, versionId, result, extra = {}) => recordAudit(pool(), log, vaConfig.storage_root, {
    artifactCode: code, operation, versionId, actor, agentViewId, result, ...extra });

  /** Operator only: runs the op and writes one audit row, success or refusal. */
  async function audited(operation, code, versionId, fn, describe = () => null) {
    validateArtifactCode(code);
    validateVersionId(versionId);
    try {
      if (!admin) throw new MiniappError('MINIAPP_ACCESS_DENIED', 'this session may not change activations');
      const r = await fn();
      await audit(operation, code, versionId, 'ok', { description: describe(r) });
      return r;
    } catch (err) {
      await audit(operation, code, versionId, 'error', { errorCode: err.code ?? 'FAILED' });
      throw err;
    }
  }

  return {
    manifestOf,

    /** `actions` ⊆ the manifest's actions; default all of them. */
    async activate(code, versionId, actions = null) {
      return audited('miniapp.activate', code, versionId, async () => {
        const manifest = await manifestOf(code, versionId);
        if (!manifest) throw new MiniappError('MANIFEST_INVALID', `the version has no valid ${MANIFEST_FILE}`);
        const allowed = actions ?? manifest.actions;
        if (!Array.isArray(allowed) || allowed.some((a) => !manifest.actions.includes(a))) {
          throw new MiniappError('ACTION_NOT_DECLARED', 'an action is not in the manifest');
        }
        const unique = [...new Set(allowed)];
        await requirePool().execute(
          `INSERT INTO miniapp_activation (artifact_code, version_id, manifest_fingerprint, allowed_actions, activated_by)
           VALUES (?, ?, ?, ?, ?)
           ON DUPLICATE KEY UPDATE manifest_fingerprint = VALUES(manifest_fingerprint),
             allowed_actions = VALUES(allowed_actions), activated_by = VALUES(activated_by), activated_at = NOW()`,
          [code, versionId, manifest.fingerprint, JSON.stringify(unique), String(actor ?? 'admin')]);
        return { artifact_code: code, version_id: versionId, manifest_fingerprint: manifest.fingerprint,
          allowed_actions: unique };
      }, (r) => `actions: ${r.allowed_actions.join(',')}`.slice(0, 255));
    },

    async deactivate(code, versionId) {
      return audited('miniapp.deactivate', code, versionId, async () => {
        const [r] = await requirePool().execute(
          'DELETE FROM miniapp_activation WHERE artifact_code = ? AND version_id = ?', [code, versionId]);
        if (!r?.affectedRows) throw new MiniappError('NOT_ACTIVATED', 'the version is not activated');
        return { artifact_code: code, version_id: versionId, activated: false };
      });
    },

    /** Operator listing: every activation row. */
    async listActivations() {
      const [rows] = await requirePool().query(
        'SELECT artifact_code, version_id, manifest_fingerprint, allowed_actions, activated_by, activated_at '
        + 'FROM miniapp_activation ORDER BY artifact_code, version_id');
      return rows.map((r) => ({ ...r, allowed_actions: jsonList(r.allowed_actions) }));
    },

    /** What a launch of this version may do. Not activated, or a manifest that no longer
     *  matches the activation, is `{activated: false}`: a files-only launch. */
    async launchSpec(code, versionId) {
      const row = await activationOf(code, versionId);
      if (!row) return { activated: false };
      const manifest = await manifestOf(code, versionId);
      if (!manifest || manifest.fingerprint !== row.manifest_fingerprint) return { activated: false };
      return { activated: true, manifest_fingerprint: row.manifest_fingerprint,
        allowed_actions: jsonList(row.allowed_actions) };
    },

    /** The activated miniapps this scope may launch now: each usable artifact's `current`. */
    async catalogue() {
      const out = [];
      for (const a of await va.listArtifacts()) {
        if (!a.current_version) continue;
        const spec = await this.launchSpec(a.artifact_code, a.current_version);
        if (!spec.activated) continue;
        const manifest = await manifestOf(a.artifact_code, a.current_version);
        out.push({ artifact_code: a.artifact_code, version_id: a.current_version, title: manifest.title });
      }
      return out;
    },
  };
}

/** The one error contract of the tools and the CLI: `{error_code, message}`. */
export function toMiniappError(err, log) {
  if (err instanceof MiniappError) return { error_code: err.code, message: err.publicMessage };
  return toToolError(err, log);
}

const ok = (payload) => ({ content: [{ type: 'text', text: JSON.stringify(payload) }] });

export async function register(server, context) {
  const { log, moduleConfigs, isToolEnabled, db, agentViewId } = context;
  const enabled = (name) => !isToolEnabled || isToolEnabled(name);
  let apps;
  try {
    apps = createMiniapps({ vaConfig: moduleConfigs?.versioned_artifacts ?? {}, db, log, agentViewId });
  } catch (err) {
    log?.('miniapps', 'ERROR', `configuration rejected: ${errorFacts(err) ?? 'unknown'}`);
    return;
  }
  const run = async (fn) => {
    try { return ok(await fn()); } catch (err) { return ok(toMiniappError(err, log)); }
  };

  if (enabled('miniapp_get_launch_spec')) {
    server.tool('miniapp_get_launch_spec',
      'Tell whether an artifact version is an activated miniapp, and which actions a launch of it may call',
      { artifact_code: z.string().regex(ARTIFACT_CODE_RE), version_id: z.string().regex(VERSION_ID_RE) },
      (args) => run(() => apps.launchSpec(args.artifact_code, args.version_id)));
  }
  if (enabled('miniapp_list')) {
    server.tool('miniapp_list',
      'List the activated miniapps whose current version this scope may launch',
      {},
      () => run(async () => ({ miniapps: await apps.catalogue() })));
  }
}
