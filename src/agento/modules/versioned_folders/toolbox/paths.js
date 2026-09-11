import path from 'node:path';
import { lstat, readdir } from 'node:fs/promises';
import { VfError, ERROR_CODES } from './errors.js';

// Exported so the tool schemas bound the SAME shape the service enforces: an
// unbounded z.string() lets an arbitrarily long identifier travel as far as the
// audit sinks before anything rejects it.
export const FOLDER_CODE_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
export const DRAFT_ID_RE = /^d-[a-z0-9]{6,32}$/;
export const VERSION_ID_RE = /^v-\d{8}-\d{6}-[a-z0-9]{4}$/;

function check(re, value, kind) {
  if (typeof value !== 'string' || !re.test(value)) throw new VfError(ERROR_CODES.INVALID_PATH, `invalid ${kind}`);
  return value;
}
export const validateFolderCode = (v) => check(FOLDER_CODE_RE, v, 'folder_code');
export const validateDraftId = (v) => check(DRAFT_ID_RE, v, 'draft_id');
export const validateVersionId = (v) => check(VERSION_ID_RE, v, 'version_id');

const segmentsOf = (p) => p.split('/').filter((s) => s !== '' && s !== '.');

/** The SYNTACTIC half of containment, split out because the read paths need exactly
 *  this and nothing else: a tree path inside the store has no filesystem root to
 *  resolve against, but it must still be rejected by the same rules a write is.
 *  Returns the CANONICAL path — repeated and trailing separators and "." segments
 *  removed — which is the form Git's `<treeish>:<path>` syntax takes and the ONLY
 *  form a caller may key a set, a projected size map or a response field by. Two
 *  spellings that resolve to one file must not survive as two identifiers.
 *
 *  Rejects the ".." SEGMENT itself — not merely paths that
 *  escape — so "deep/nested/../file.txt" is refused even though it normalizes back
 *  inside. Banning the segment is what the Global Constraint says and keeps this
 *  check independent of normalization subtleties. */
export function validateRelPath(relPath) {
  if (typeof relPath !== 'string' || relPath === '') throw new VfError(ERROR_CODES.INVALID_PATH, 'path must be a non-empty string');
  if (path.isAbsolute(relPath) || /^[\\/]/.test(relPath)) throw new VfError(ERROR_CODES.INVALID_PATH, 'absolute paths are not allowed');
  // A backslash is REFUSED, not treated as a separator. Accepting it made
  // "a\\b.txt" and "a/b.txt" two spellings of one file: containment resolved both to
  // the same place while every caller-facing set and response field kept the
  // original spelling, so one batch could claim two outcomes for one path.
  // Canonicalizing it silently would rewrite a caller's path across a character that
  // means something different on another platform; the contract is forward slashes,
  // so say so.
  if (relPath.includes('\\')) throw new VfError(ERROR_CODES.INVALID_PATH, 'paths use forward slashes');
  const segments = segmentsOf(relPath);
  if (segments.length === 0) throw new VfError(ERROR_CODES.INVALID_PATH, 'path must name a file');
  if (segments.includes('..')) throw new VfError(ERROR_CODES.INVALID_PATH, 'parent traversal is not allowed');
  if (segments[0] === '.git') throw new VfError(ERROR_CODES.INVALID_PATH, 'internal metadata is not accessible');
  if (segments.some((s) => s.includes('\0'))) throw new VfError(ERROR_CODES.INVALID_PATH, 'invalid character in path');
  return segments.join('/');
}

/** Syntactic containment resolved against a real filesystem root. */
export function safeJoin(root, relPath) {
  const segments = validateRelPath(relPath).split('/');

  const resolvedRoot = path.resolve(root);
  const resolved = path.resolve(resolvedRoot, segments.join(path.sep));
  if (resolved !== resolvedRoot && !resolved.startsWith(resolvedRoot + path.sep)) {
    throw new VfError(ERROR_CODES.PATH_OUTSIDE_FOLDER, 'path escapes the folder');
  }
  return resolved;
}

/** Syntactic containment PLUS an lstat of every EXISTING component. safeJoin alone
 *  cannot see a symlinked ancestor: "link/out.txt" contains no "..", resolves inside
 *  the root as a string, and still writes outside it.
 *
 *  An `allowSymlinks` option is accepted for forward compatibility and currently
 *  changes NOTHING: MVP supports only `false` (D-7), and the service rejects a
 *  configured `true` outright. Returning early on `true` would turn a config value
 *  into a containment bypass, so this function stays strict regardless. The option
 *  is deliberately NOT destructured — a binding the body never reads is an
 *  ESLint `no-unused-vars` warning, and `_`-prefixing a named property to silence
 *  it reads as an oversight rather than a decision. */
export async function safeResolve(root, relPath, _opts = {}) {
  const resolved = safeJoin(root, relPath);
  const resolvedRoot = path.resolve(root);
  let current = resolvedRoot;
  for (const segment of path.relative(resolvedRoot, resolved).split(path.sep)) {
    current = path.join(current, segment);
    let st;
    // ONLY "it isn't there" ends the walk. EACCES, ELOOP, ENOTDIR or an I/O
    // error are not evidence that the rest of the path is absent — swallowing
    // them would skip the symlink check for every remaining component, which is
    // the one thing this function exists to do. Fail closed instead.
    try { st = await lstat(current); }
    catch (err) {
      if (err?.code === 'ENOENT') break;          // nothing below exists
      throw new VfError(ERROR_CODES.INVALID_PATH, 'path could not be verified', { cause: err });
    }
    if (st.isSymbolicLink()) throw new VfError(ERROR_CODES.SYMLINK_NOT_ALLOWED, 'symbolic links are not allowed in this folder');
  }
  return resolved;
}

/** "Not there" is [] — anything else throws. A blanket `.catch(() => [])` turns an
 *  EACCES or an I/O error into "the directory is empty", which makes a sweep that
 *  cleans nothing indistinguishable from a sweep that had nothing to clean. The
 *  callers here are reclamation paths: silently doing nothing is the failure mode
 *  they exist to prevent. */
export async function listDirOrEmpty(dir) {
  try { return await readdir(dir, { withFileTypes: true }); }
  catch (err) { if (err?.code === 'ENOENT') return []; throw err; }
}

export const folderRoot = (storageRoot, code) => path.join(storageRoot, validateFolderCode(code));
export const repoDir = (storageRoot, code) => path.join(folderRoot(storageRoot, code), 'repo.git');
export const draftDir = (storageRoot, code, draftId) =>
  path.join(folderRoot(storageRoot, code), 'worktrees', validateDraftId(draftId));
