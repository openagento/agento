import { it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, mkdir, writeFile, utimes, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { withLock, sweepStaleLocks } from '../../../modules/versioned_artifacts/toolbox/locking.js';

let dir;
beforeEach(async () => { dir = await mkdtemp(path.join(tmpdir(), 'vf-lock-')); });
afterEach(async () => { await rm(dir, { recursive: true, force: true }); });

it('serializes two concurrent holders', async () => {
  const lock = path.join(dir, 'a.lock'); const order = [];
  await Promise.all([
    withLock(lock, async () => { order.push('a-in'); await new Promise(r => setTimeout(r, 60)); order.push('a-out'); }),
    withLock(lock, async () => { order.push('b-in'); order.push('b-out'); }),
  ]);
  // Serialization is the property under test — WHICH holder acquires first is not.
  // `mkdir` is atomic, but two concurrent acquires are two libuv threadpool jobs
  // and either may complete first, so asserting a fixed winner makes this test
  // fail under load and pass in isolation. Assert instead that whoever went first
  // finished before the other started.
  const first = order[0] === 'a-in' ? ['a-in', 'a-out', 'b-in', 'b-out'] : ['b-in', 'b-out', 'a-in', 'a-out'];
  expect(order).toEqual(first);
});

it('releases the lock when the body throws', async () => {
  const lock = path.join(dir, 'b.lock');
  await expect(withLock(lock, async () => { throw new Error('boom'); })).rejects.toThrow('boom');
  await expect(withLock(lock, async () => 'ok')).resolves.toBe('ok');
});

it('throws DRAFT_LOCKED when the wait times out', async () => {
  const lock = path.join(dir, 'c.lock');
  await mkdir(lock); await writeFile(path.join(lock, 'owner'), 'someone-else');
  await expect(withLock(lock, async () => 'never', { timeoutMs: 150 })).rejects.toThrow(/DRAFT_LOCKED/);
});

it('never breaks a stale lock at runtime', async () => {
  const lock = path.join(dir, 'd.lock');
  await mkdir(lock); await writeFile(path.join(lock, 'owner'), 'dead-process');
  const old = new Date(Date.now() - 600_000);
  await utimes(lock, old, old);
  // Age is NOT a licence to delete: breaking is a read-then-delete pair and can
  // destroy a lock a third process acquired in between. Stale locks are cleared
  // only by sweepStaleLocks(), at startup.
  await expect(withLock(lock, async () => 'ok', { timeoutMs: 150 })).rejects.toThrow(/DRAFT_LOCKED/);
  await expect(stat(lock)).resolves.toBeTruthy();   // untouched
});

it('a heartbeat keeps a live holder\'s lock fresh so the sweep spares it', async () => {
  const lock = path.join(dir, '.locks', 'site', 'e.lock');   // the REAL layout: <root>/.locks/<artifact_code>/
  const held = withLock(lock, async () => { await new Promise(r => setTimeout(r, 400)); return 'done'; },
    { heartbeatMs: 50 });
  await new Promise(r => setTimeout(r, 250));
  expect(await sweepStaleLocks(dir, { staleMs: 200 })).toEqual([]);   // live holder spared
  await expect(stat(lock)).resolves.toBeTruthy();
  expect(await held).toBe('done');
});

it('sweepStaleLocks clears an abandoned lock and leaves it acquirable', async () => {
  const lock = path.join(dir, '.locks', 'site', 'g.lock');
  await mkdir(path.dirname(lock), { recursive: true });
  await mkdir(lock); await writeFile(path.join(lock, 'owner'), 'dead-process');
  const old = new Date(Date.now() - 600_000);
  await utimes(lock, old, old);
  expect(await sweepStaleLocks(dir, { staleMs: 300_000 })).toEqual([lock]);
  await expect(withLock(lock, async () => 'ok', { timeoutMs: 1000 })).resolves.toBe('ok');
});

it('a superseded holder does not delete the new holder\'s lock', async () => {
  // The release path is still token-guarded, because a sweep can legitimately
  // remove a lock whose holder is merely wedged rather than dead. Here A is
  // still ALIVE inside its body when its lock is swept and B acquires — the
  // race the previous version of this test could not reproduce, because it had
  // no live A to run a `finally`.
  const lock = path.join(dir, 'f.lock');
  let bAcquired;
  const a = withLock(lock, async () => {
    await rm(lock, { recursive: true, force: true });          // simulate the sweep
    bAcquired = withLock(lock, async () => { await new Promise(r => setTimeout(r, 120)); return 'b'; });
    await new Promise(r => setTimeout(r, 40));                 // A now releases while B holds
    return 'a';
  }, { timeoutMs: 1000 });
  expect(await a).toBe('a');
  await expect(stat(lock)).resolves.toBeTruthy();   // A did NOT delete B's lock
  expect(await bAcquired).toBe('b');
});
