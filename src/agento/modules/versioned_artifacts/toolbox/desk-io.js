import fs from 'node:fs';
import path from 'node:path';
import { ArtifactError, ERROR_CODES } from './errors.js';
import { ARTIFACT_CODE_RE, DRAFT_ID_RE, VERSION_ID_RE } from './paths.js';

/** The bind-mount point, and the ONLY trust anchor on the desk side.
 *
 *  Not the job directory: the sandbox mounts `../workspace:/workspace` read-write, so
 *  `<ws>`, `<av>` and `<job_id>` are all agent-writable, and an assert rooted at the job
 *  dir proves nothing the moment the job dir is itself a symlink to `/`. The mount pins
 *  this path; everything below it is walked, never resolved. */
export const ARTIFACTS_ROOT = '/workspace/artifacts';

const { O_RDONLY, O_DIRECTORY, O_NOFOLLOW, O_NONBLOCK, O_WRONLY, O_CREAT, O_EXCL } = fs.constants;

const SESSION_SEGMENTS = 3;          // <ws>/<av>/<job_id>, the shape server.js builds
const DESK_SEGMENT = 'versioned-artifacts';

/** `/proc/self/fd/<dirfd>/<name>` is `openat(2)` with no native module: the kernel
 *  resolves it from the descriptor's own inode instead of re-walking the path.
 *
 *  `name` MUST be a single component. Measured: a multi-component name IS resolved
 *  normally from that point, so `.../<fd>/link/passwd` follows `link` and O_NOFOLLOW —
 *  which only ever applies to the FINAL component — would not see it. Single components
 *  are what makes O_NOFOLLOW cover the whole name. */
function at(fd, name) {
  if (typeof name !== 'string' || name === '' || name === '.' || name === '..'
      || name.includes('/') || name.includes('\0')) {
    throw new ArtifactError(ERROR_CODES.INVALID_PATH, 'not a single path component');
  }
  return `/proc/self/fd/${fd}/${name}`;
}

/** `/proc/self/fd` IS the mechanism, and the desk exists only inside the toolbox
 *  container, which is always Linux. Refusing at CALL time rather than on import is
 *  deliberate: `tool-declaration.test.js` executes every module's `register()` on the
 *  developer's host, so an import-time throw would take the whole suite red on macOS. */
function requireLinux() {
  if (process.platform !== 'linux') {
    throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED,
      'the desk is only available inside the toolbox container');
  }
}

/** An open refused by O_NOFOLLOW reports ENOTDIR (with O_DIRECTORY) or ELOOP (without),
 *  and ENOTDIR also means "a regular file where a directory belongs". The open has
 *  already failed either way; this lstat only decides which refusal to NAME, so an
 *  operator reads the actual cause instead of a guess. */
function refusal(fd, name, err) {
  if (err?.code === 'ENOENT') return null;                 // caller decides: create, or DESK_MISSING
  if (err?.code === 'ENOTDIR' || err?.code === 'ELOOP') {
    let st;
    try { st = fs.lstatSync(at(fd, name)); } catch { st = null; }
    if (st?.isSymbolicLink()) {
      return new ArtifactError(ERROR_CODES.SYMLINK_NOT_ALLOWED, 'symbolic links are not allowed in this artifact');
    }
    return new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'a desk component is not a directory');
  }
  return new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the desk could not be opened', { cause: err });
}

export function openDirAt(fd, name) {
  try { return fs.openSync(at(fd, name), O_RDONLY | O_DIRECTORY | O_NOFOLLOW); }
  catch (err) { const e = refusal(fd, name, err); if (e) throw e; return null; }
}

/** O_NONBLOCK on the READ is load-bearing. Opening a FIFO O_RDONLY blocks until a writer
 *  arrives, and it blocks *inside* `open` — before the `fstat` mode check can reject it.
 *  This module is synchronous, so a blocked open hangs the whole toolbox event loop for
 *  every session, and `mkfifo` needs no privilege. With the flag the open returns a
 *  descriptor whose `fstat` reports the FIFO, rejectable before a byte is read. */
export function openFileAt(fd, name, write = false) {
  const flags = write ? (O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW) : (O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
  try { return fs.openSync(at(fd, name), flags, 0o600); }
  catch (err) { const e = refusal(fd, name, err); if (e) throw e; return null; }
}

export const mkdirAt = (fd, name) => {
  try { fs.mkdirSync(at(fd, name), 0o700); }
  catch (err) { if (err?.code !== 'EEXIST') throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the desk could not be created', { cause: err }); }
};
export const readdirAt = (fd, name) =>
  fs.readdirSync(name === undefined ? `/proc/self/fd/${fd}` : at(fd, name), { withFileTypes: true });
export const unlinkAt = (fd, name) => fs.unlinkSync(at(fd, name));
export const rmdirAt = (fd, name) => fs.rmdirSync(at(fd, name));
export const closeDesk = (fd) => { try { fs.closeSync(fd); } catch { /* already closed */ } };

/** The session components come from `artifactsDir`, which supplies NAMES only — the walk
 *  supplies the trust. A missing or `_fallback` session means the toolbox never learned
 *  which job it is serving; writing to a shared fallback desk would mix two jobs' bytes. */
function sessionSegments(artifactsDir) {
  const unavailable = (why) => new ArtifactError(ERROR_CODES.WORKSPACE_UNAVAILABLE, why);
  if (typeof artifactsDir !== 'string' || artifactsDir === '') throw unavailable('this session has no workspace');
  const rel = path.relative(ARTIFACTS_ROOT, path.resolve(artifactsDir));
  if (rel === '' || rel.startsWith('..') || path.isAbsolute(rel)) throw unavailable('this session has no workspace');
  const segments = rel.split(path.sep);
  if (segments.length !== SESSION_SEGMENTS || segments.includes('_fallback')) {
    throw unavailable('this session has no workspace');
  }
  return segments;
}

const checked = (re, value, kind) => {
  if (typeof value !== 'string' || !re.test(value)) throw new ArtifactError(ERROR_CODES.INVALID_PATH, `invalid ${kind}`);
  return value;
};

/** A desk is keyed by whatever was materialized into it — a draft for `create_draft` and
 *  `save_version`, a version for a read. Both shapes, nothing else. */
const deskId = (id) => {
  if (typeof id === 'string' && (DRAFT_ID_RE.test(id) || VERSION_ID_RE.test(id))) return id;
  throw new ArtifactError(ERROR_CODES.INVALID_PATH, 'invalid draft_id or version_id');
};

/** The desk's path as a STRING — for the ANSWER a tool gives the agent, never for I/O.
 *  Every operation reaches the desk through `openDesk`'s descriptor chain; this only
 *  spells out where that chain ended, and it validates the same three inputs, so a
 *  caller that builds it BEFORE mutating anything refuses a bad session first. */
export const deskPath = (artifactsDir, artifactCode, id) => path.join(
  ARTIFACTS_ROOT, ...sessionSegments(artifactsDir), DESK_SEGMENT,
  checked(ARTIFACT_CODE_RE, artifactCode, 'artifact_code'), deskId(id),
);

/** The session check alone, for a caller that must refuse BEFORE it writes to the store:
 *  `create_draft` would otherwise leave an open draft on a session that can hold no desk. */
export const requireSession = (artifactsDir) => { sessionSegments(artifactsDir); };

/** Walk from the mount point to the desk, holding a descriptor at every step, and return
 *  the desk's own descriptor. A component that is a symlink AT OPEN TIME is refused by
 *  O_NOFOLLOW; a component replaced AFTER its open is irrelevant, because the descriptor
 *  already pins the inode. That is the whole containment argument — there is no
 *  `realpath` anywhere on this side, and nothing below re-resolves the desk by name.
 *
 *  `create` is explicit and has no default on purpose. `save_version` mirrors the desk
 *  and deletes worktree files absent from it, so silently creating an empty desk would
 *  read as "the agent deleted every file" and mint an empty version over a good one —
 *  data loss reported as success. Absence and emptiness are different states. */
export function openDesk(artifactsDir, artifactCode, id, { create } = {}) {
  requireLinux();
  if (typeof create !== 'boolean') {
    throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'openDesk needs an explicit create flag');
  }
  const chain = [
    ...sessionSegments(artifactsDir),
    DESK_SEGMENT,
    checked(ARTIFACT_CODE_RE, artifactCode, 'artifact_code'),
    deskId(id),
  ];

  let fd;
  try { fd = fs.openSync(ARTIFACTS_ROOT, O_RDONLY | O_DIRECTORY | O_NOFOLLOW); }
  catch (err) { throw new ArtifactError(ERROR_CODES.WORKSPACE_UNAVAILABLE, 'this session has no workspace', { cause: err }); }

  try {
    for (const segment of chain) {
      let next = openDirAt(fd, segment);
      if (next === null) {
        if (!create) throw new ArtifactError(ERROR_CODES.DESK_MISSING, 'the desk is not there — materialize it first');
        mkdirAt(fd, segment);
        next = openDirAt(fd, segment);
        if (next === null) throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the desk vanished while it was being created');
      }
      closeDesk(fd);
      fd = next;
    }
    return fd;
  } catch (err) { closeDesk(fd); throw err; }
}

/** `fs.rmSync(recursive)` on a path re-resolves every component as it descends, which
 *  reopens the window `openDesk` closed. This descends on descriptors instead: a symlink
 *  is `unlinkAt`ed as a link and never followed, and a FIFO or socket is removed without
 *  ever being opened. The desk directory itself survives — a materialize replaces
 *  contents, it does not remove the desk. */
export function emptyDesk(fd) {
  for (const entry of readdirAt(fd)) {
    if (entry.isDirectory()) {
      const child = openDirAt(fd, entry.name);
      // A planted symlink never reaches this branch — measured: Dirent.isDirectory() is
      // FALSE for a symlink-to-directory, because it reflects d_type, so a link falls
      // straight through to `unlinkAt` and the LINK, not its target, is removed.
      // The re-open is for the RACE: an entry that was a real directory at readdir time
      // and is a symlink by the time we descend. O_NOFOLLOW refuses that one, and the
      // refusal THROWS — clearing the desk fails closed rather than deleting through a
      // link. `null` is the other race and the only one: the entry is already gone, so
      // there is nothing left to remove.
      if (child === null) continue;
      try { emptyDesk(child); } finally { closeDesk(child); }
      rmdirAt(fd, entry.name);
      continue;
    }
    unlinkAt(fd, entry.name);
  }
}

const notRegular = () => new ArtifactError(ERROR_CODES.INVALID_PATH, 'only regular files and directories are stored');

/** Read a descriptor to EOF, enforcing the limits on the bytes ACTUALLY READ.
 *
 *  Not on a prior `fstat().size`: the desk is agent-owned, so a file that is small when
 *  stat'ed can be large when read, and the check would pass on a number the agent
 *  invalidated a moment later. Counting here also aborts a huge file part-way instead of
 *  buffering it first. */
function readAll(fd, limits, counters) {
  const chunk = Buffer.allocUnsafe(64 * 1024);
  const parts = [];
  let size = 0;
  for (;;) {
    const n = fs.readSync(fd, chunk, 0, chunk.length, null);
    if (n === 0) break;
    size += n;
    counters.bytes += n;
    if (limits.max_file_size != null && size > limits.max_file_size) {
      throw new ArtifactError(ERROR_CODES.FILE_TOO_LARGE, 'file exceeds the size limit');
    }
    if (limits.max_total_size != null && counters.bytes > limits.max_total_size) {
      throw new ArtifactError(ERROR_CODES.ARTIFACT_TOO_LARGE, 'artifact exceeds the total size limit');
    }
    parts.push(Buffer.from(chunk.subarray(0, n)));
  }
  return Buffer.concat(parts);
}

/** Collect the desk into memory BEFORE anything is written to the store, so a refused
 *  limit leaves the worktree untouched rather than holding a partial mirror the commit
 *  would then freeze into an immutable version. `max_total_size` bounds the buffer. */
function collect(fd, rel, out, limits, counters) {
  for (const entry of readdirAt(fd)) {
    const name = entry.name;
    // The worktree's `.git` is the pointer `worktree add` writes, and a `.git` on the
    // desk is agent-supplied. Neither crosses.
    if (rel === '' && name === '.git') continue;
    const childRel = rel ? `${rel}/${name}` : name;

    if (entry.isDirectory()) {
      const child = openDirAt(fd, name);
      if (child === null) continue;             // vanished between readdir and open
      out.dirs.push(childRel);
      try { collect(child, childRel, out, limits, counters); } finally { closeDesk(child); }
      continue;
    }

    const file = openFileAt(fd, name);
    if (file === null) continue;                // vanished; nothing to mirror
    try {
      // The mode check happens on the DESCRIPTOR, after O_NOFOLLOW has already refused a
      // symlink and O_NONBLOCK has stopped a fifo from blocking the open.
      if (!fs.fstatSync(file).isFile()) throw notRegular();
      counters.files += 1;
      if (limits.max_files != null && counters.files > limits.max_files) {
        throw new ArtifactError(ERROR_CODES.TOO_MANY_FILES, 'too many files');
      }
      out.files.push({ rel: childRel, data: readAll(file, limits, counters) });
    } finally { closeDesk(file); }
  }
}

/** Recursive removal WITHOUT `fs.rmSync`, so this module holds no path-based recursive
 *  delete at all — the property `desk-containment.test.js` asserts structurally. */
function removeTree(abs) {
  for (const entry of fs.readdirSync(abs, { withFileTypes: true })) {
    const child = path.join(abs, entry.name);
    if (entry.isDirectory()) removeTree(child); else fs.unlinkSync(child);
  }
  fs.rmdirSync(abs);
}

function prune(dest, keepFiles, keepDirs, rel = '') {
  for (const entry of fs.readdirSync(rel ? path.join(dest, rel) : dest, { withFileTypes: true })) {
    if (rel === '' && entry.name === '.git') continue;
    const childRel = rel ? `${rel}/${entry.name}` : entry.name;
    const abs = path.join(dest, childRel);
    if (entry.isDirectory()) {
      if (keepDirs.has(childRel)) prune(dest, keepFiles, keepDirs, childRel);
      else removeTree(abs);
    } else if (!keepFiles.has(childRel)) fs.unlinkSync(abs);
  }
}

/** The desk → the store. This is the ONE operation that reads agent-owned bytes into an
 *  immutable version, so every rule of the walk applies: each entry is opened
 *  `O_NOFOLLOW` relative to its parent descriptor, `fstat`ed, non-regular entries are
 *  refused, and the limits are counted on bytes read. `dest` is the draft worktree — the
 *  trusted store side, where `safeResolve` already guarantees the root. */
export function mirrorOut(deskFd, dest, limits = {}) {
  requireLinux();
  const out = { dirs: [], files: [] };
  collect(deskFd, '', out, limits, { files: 0, bytes: 0 });

  prune(dest, new Set(out.files.map((f) => f.rel)), new Set(out.dirs));
  for (const dir of out.dirs) fs.mkdirSync(path.join(dest, dir), { recursive: true });
  for (const file of out.files) fs.writeFileSync(path.join(dest, file.rel), file.data);
}

/** The store → the desk. `src` is toolbox-owned (a draft worktree, or a version already
 *  extracted into the store's own `.tmp`), so it is read by path; the DESK side is
 *  written only through descriptors. Two flags, two distinct refusals — measured, not
 *  assumed: `O_NOFOLLOW` is what refuses a planted SYMLINK (dropping `O_EXCL` alone does
 *  not weaken that), while `O_EXCL` refuses a PRE-EXISTING entry. The caller clears the
 *  desk with `emptyDesk` first, so an EEXIST here means the desk changed underneath and
 *  the copy must not continue on top of bytes it did not put there. */
export function mirrorIn(src, destFd, top = true) {
  requireLinux();
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (top && entry.name === '.git') continue;
    const from = path.join(src, entry.name);

    if (entry.isDirectory()) {
      mkdirAt(destFd, entry.name);
      const child = openDirAt(destFd, entry.name);
      if (child === null) throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the desk changed while it was being written');
      try { mirrorIn(from, child, false); } finally { closeDesk(child); }
      continue;
    }
    if (!entry.isFile()) throw notRegular();

    const file = openFileAt(destFd, entry.name, true);
    if (file === null) throw new ArtifactError(ERROR_CODES.STORAGE_OPERATION_FAILED, 'the desk changed while it was being written');
    try { fs.writeFileSync(file, fs.readFileSync(from)); } finally { closeDesk(file); }
  }
}
