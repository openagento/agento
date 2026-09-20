import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile, rm, stat, utimes } from 'node:fs/promises';
import { ArtifactError, ERROR_CODES } from './errors.js';
import { listDirOrEmpty } from './paths.js';

const POLL_MS = 25;

/**
 * Serialize a mutation behind a filesystem lock inside the storage root, so every
 * process reaching the store observes it (PRD §28 — an in-process mutex would be
 * scoped to one module instance of one MCP session).
 *
 * There is NO runtime stale-breaking. `mkdir` gives atomic acquire, not atomic
 * break: breaking is a read-then-delete pair, and a third process can acquire in
 * the window between them. A held lock is simply held; an abandoned one is cleared
 * by sweepStaleLocks() at startup, where there is no concurrent acquirer to race.
 *
 * Takes no logger by design — see the plan's Task 4 note.
 */
export async function withLock(lockPath, fn, { timeoutMs = 10_000, heartbeatMs = 30_000 } = {}) {
  // Neither <storageRoot>/.locks/ nor <artifact>/locks/ is created by any other step.
  // Without this, the first lock of a fresh store fails ENOENT — which is not
  // DRAFT_LOCKED but a hard failure the caller cannot act on. The parent is always
  // a path the service validated, never one derived from agent input.
  await mkdir(path.dirname(lockPath), { recursive: true });

  const token = randomUUID();
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      await mkdir(lockPath);
      break;
    } catch (err) {
      if (err?.code !== 'EEXIST') throw err;
      if (Date.now() >= deadline) {
        throw new ArtifactError(ERROR_CODES.DRAFT_LOCKED, 'another operation is in progress for this draft');
      }
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
  }
  await writeFile(path.join(lockPath, 'owner'), token, 'utf8');

  // Keeps the mtime fresh so sweepStaleLocks can tell a live holder — in any
  // process, including the admin CLI — from a dead one.
  const beat = setInterval(() => {
    const now = new Date();
    utimes(lockPath, now, now).catch(() => { /* swept or removed; release handles it */ });
  }, heartbeatMs);
  if (typeof beat.unref === 'function') beat.unref();

  try {
    return await fn();
  } finally {
    clearInterval(beat);
    let owner = null;
    try { owner = await readFile(path.join(lockPath, 'owner'), 'utf8'); }
    catch { owner = null; }        // swept, or already gone — same case, not an error
    // Only the still-current holder may remove the directory. If a sweep removed
    // this lock and someone else now owns the name, leave it alone.
    if (owner === token) await rm(lockPath, { recursive: true, force: true });
  }
}

export async function sweepStaleLocks(storageRoot, { staleMs = 300_000 } = {}) {
  // Every lock lives under `.locks`: a per-artifact `<code>/lifecycle.lock` one level
  // down, plus the flat `owner-<id>.lock`. A lock is a directory whose name ends in
  // `.lock`; a bare `<code>/` container is descended, never swept.
  const base = path.join(storageRoot, '.locks');
  const locks = [];
  for (const e of await listDirOrEmpty(base)) {
    if (!e.isDirectory()) continue;
    const full = path.join(base, e.name);
    if (e.name.endsWith('.lock')) { locks.push(full); continue; }
    for (const f of await listDirOrEmpty(full)) {
      if (f.isDirectory() && f.name.endsWith('.lock')) locks.push(path.join(full, f.name));
    }
  }
  const removed = [];
  for (const lock of locks) {
    // An EACCES must NOT read as "the lock disappeared": a reclamation path that cannot
    // inspect a lock has to say so, not report a clean sweep. Only ENOENT ends it.
    let st;
    try { st = await stat(lock); }
    catch (err) { if (err?.code === 'ENOENT') continue; throw err; }
    if (Date.now() - st.mtimeMs > staleMs) { await rm(lock, { recursive: true, force: true }); removed.push(lock); }
  }
  return removed;
}
