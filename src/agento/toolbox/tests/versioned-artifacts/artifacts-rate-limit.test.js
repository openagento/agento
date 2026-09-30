import { describe, it, expect } from 'vitest';
import { createWindowCounter, createRecentSet } from '../../../modules/versioned_artifacts/server/rate-limit.js';

describe('createWindowCounter', () => {
  it('counts to the limit within a window and forgets after it', () => {
    let t = 0;
    const c = createWindowCounter({ limit: 2, windowMs: 1000, now: () => t });
    expect(c.take('a')).toBe(true);
    expect(c.take('a')).toBe(true);
    expect(c.take('a')).toBe(false);
    t = 1000;
    expect(c.take('a')).toBe(true);
  });

  it('gives a reserved count back with refund()', () => {
    const c = createWindowCounter({ limit: 1, windowMs: 1000, now: () => 0 });
    expect(c.take('a')).toBe(true);
    c.refund('a');
    expect(c.take('a')).toBe(true);
    expect(c.take('a')).toBe(false);
  });

  it('returns to size 0 after one window, and never grows past its cap', () => {
    let t = 0;
    const c = createWindowCounter({ limit: 5, windowMs: 1000, maxKeys: 3, now: () => t });
    for (const k of ['a', 'b', 'c']) expect(c.take(k)).toBe(true);
    expect(c.take('d')).toBe(false); // full: a new key is refused, not stored
    expect(c.size()).toBe(3);
    expect(c.take('a')).toBe(true); // a known key under its limit still passes
    t = 1000;
    c.sweep();
    expect(c.size()).toBe(0);
    expect(c.take('d')).toBe(true);
  });
});

describe('createRecentSet', () => {
  it('remembers a key for its ttl and holds at most maxKeys', () => {
    let t = 0;
    const r = createRecentSet({ ttlMs: 1000, maxKeys: 1, now: () => t });
    r.add('a');
    r.add('b'); // full: not stored
    expect([r.has('a'), r.has('b')]).toEqual([true, false]);
    t = 1000;
    expect(r.has('a')).toBe(false);
    r.add('b'); // the expired key made room
    expect(r.has('b')).toBe(true);
  });
});
