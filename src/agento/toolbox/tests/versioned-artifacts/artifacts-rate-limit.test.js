import { describe, it, expect } from 'vitest';
import { createWindowCounter } from '../../../modules/versioned_artifacts/server/rate-limit.js';

describe('createWindowCounter', () => {
  it('counts to the limit within a window and forgets after it', () => {
    let t = 0;
    const c = createWindowCounter({ limit: 2, windowMs: 1000, now: () => t });
    expect(c.take('a')).toBe(true);
    expect(c.take('a')).toBe(true);
    expect(c.take('a')).toBe(false);
    expect(c.allowed('a')).toBe(false);
    t = 1000;
    expect(c.allowed('a')).toBe(true);
  });

  it('returns to size 0 after one window, and never grows past its cap', () => {
    let t = 0;
    const c = createWindowCounter({ limit: 5, windowMs: 1000, maxKeys: 3, now: () => t });
    for (const k of ['a', 'b', 'c']) expect(c.take(k)).toBe(true);
    expect(c.take('d')).toBe(false); // full: a new key is refused, not stored
    expect(c.size()).toBe(3);
    expect(c.allowed('d')).toBe(true); // a read never inserts
    t = 1000;
    c.sweep();
    expect(c.size()).toBe(0);
    expect(c.take('d')).toBe(true);
  });
});
