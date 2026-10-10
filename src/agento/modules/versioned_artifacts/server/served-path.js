import { ARTIFACT_CODE_RE, VERSION_ID_RE } from '../toolbox/paths.js';

// The artifacts server's side of the one-parser rule (PRD E6 §6.2). An app path was
// already parsed by web (src/agento/web/app_path.py), and this only checks that it got
// that canonical form back: it never repairs a path, so a path web did not produce cannot
// reach the filesystem. A share path comes straight from the browser (through the proxy's
// `/s/<token>{uri}` rewrite), and this is the only parser it meets.
// Contract fixture: tests/fixtures/app_path_v1.json.

export const SHARE_TOKEN_RE = /^[0-9a-f]{32}$/;
const CONTROL = /[\x00-\x1f\x7f]/;

/** One decoded path segment, or null. Decoded exactly once; a bad escape is a refusal. */
function decodeSegment(raw) {
  let seg;
  try { seg = decodeURIComponent(raw); } catch { return null; }
  if (seg === '' || seg.startsWith('.') || seg.includes('/') || seg.includes('\\') || CONTROL.test(seg)) return null;
  return seg;
}

/** The same quoting as Python's `urllib.parse.quote(seg, safe="")`. */
export const encodeSegment = (seg) =>
  encodeURIComponent(seg).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);

function tail(raws, canonical) {
  const trailing = raws.at(-1) === '';
  const names = trailing ? raws.slice(0, -1) : raws;
  const decoded = [];
  for (const raw of names) {
    const seg = decodeSegment(raw);
    if (seg === null || (canonical && encodeSegment(seg) !== raw)) return null;
    decoded.push(seg);
  }
  return { names: decoded, trailing, last: names.at(-1) ?? null };
}

/**
 * `{kind: 'health'}` for `/`; `{kind: 'app', code, versionId, names, trailing, last}` for
 * `/<code>/v/<id>/…`; `{kind: 'share', token, versionId, names, trailing, last}` for
 * `/s/<token>/…` (`versionId` null means `current`); otherwise null.
 */
export function parseServedPath(rawUrl) {
  if (typeof rawUrl !== 'string') return null;
  const raw = rawUrl.split('?', 1)[0];
  if (raw === '/') return { kind: 'health' };
  if (!raw.startsWith('/') || CONTROL.test(raw)) return null;
  const parts = raw.slice(1).split('/');
  if (parts[0] === 's' && SHARE_TOKEN_RE.test(parts[1] ?? '') && parts.length >= 3) {
    const t = tail(parts.slice(2), false);
    if (t === null) return null;
    if (t.names[0] === 'v' && VERSION_ID_RE.test(t.names[1] ?? '')) {
      return { kind: 'share', token: parts[1], versionId: t.names[1], ...t, names: t.names.slice(2) };
    }
    return { kind: 'share', token: parts[1], versionId: null, ...t };
  }
  if (parts.length < 4 || !ARTIFACT_CODE_RE.test(parts[0]) || parts[1] !== 'v' || !VERSION_ID_RE.test(parts[2])) return null;
  const t = tail(parts.slice(3), true);
  return t === null ? null : { kind: 'app', code: parts[0], versionId: parts[2], ...t };
}
