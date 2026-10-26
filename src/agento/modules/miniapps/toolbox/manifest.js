// The miniapp manifest (`miniapp.json` at a version's root): the one strict parser.

export const MANIFEST_FILE = 'miniapp.json';
export const MAX_MANIFEST_BYTES = 64 * 1024;
const MAX_ACTIONS = 64;
// The tool-name shape the invoke route accepts.
const ACTION_RE = /^[a-z0-9_]{1,64}$/;
const MANIFEST_KEYS = ['schema', 'title', 'actions'];

/** The manifest, strict: unknown keys, a wrong type or a duplicate action is null. */
export function parseManifest(bytes) {
  let m;
  // Fatal: bytes that are not UTF-8 are refused, never read as U+FFFD.
  try { m = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); } catch { return null; }
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
