// Fixed-window counters for the artifacts server (RULES.md SEC-12). Plain `node:http`
// here, so not the toolbox's express-rate-limit. Memory is bounded: an expired entry is
// dropped when read, `sweep()` drops every expired one, and a store holds at most
// `maxKeys` — at the cap, after a sweep, a new key is refused by `take()` (fail closed
// under a flood). A caller reserves with `take()` before any work and gives the count back
// with `refund()` when the attempt was not a failure, so concurrent requests cannot all
// pass the limit before one of them is counted.
export const WINDOW_MS = 60_000;
export const AUTH_FAILURES_PER_ADDRESS = 60;
export const REQUESTS_PER_CREDENTIAL = 600;
export const MAX_KEYS = 10_000;
// How long a credential that authenticated skips the per-address failure check.
export const RECENT_MS = 15 * 60_000;

export function createWindowCounter({ limit, windowMs = WINDOW_MS, maxKeys = MAX_KEYS, now = Date.now }) {
  const store = new Map();
  const live = (key) => {
    const e = store.get(key);
    if (e && e.resetAt <= now()) { store.delete(key); return undefined; }
    return e;
  };
  const sweep = () => {
    const t = now();
    for (const [k, e] of store) if (e.resetAt <= t) store.delete(k);
  };
  const full = () => {
    if (store.size >= maxKeys) sweep();
    return store.size >= maxKeys;
  };
  return {
    /** Count one for `key`; false when that is over the limit, or the store is full. */
    take(key) {
      let e = live(key);
      if (!e) {
        if (full()) return false;
        e = { count: 0, resetAt: now() + windowMs };
        store.set(key, e);
      }
      e.count += 1;
      return e.count <= limit;
    },
    /** Give back one count that `take()` reserved. */
    refund(key) {
      const e = live(key);
      if (e && e.count > 0) e.count -= 1;
    },
    sweep,
    size: () => store.size,
  };
}

/** Keys seen recently (a credential that authenticated), each for `ttlMs`, at most `maxKeys`. */
export function createRecentSet({ ttlMs = RECENT_MS, maxKeys = MAX_KEYS, now = Date.now } = {}) {
  const store = new Map();
  const sweep = () => {
    const t = now();
    for (const [k, until] of store) if (until <= t) store.delete(k);
  };
  return {
    has(key) {
      const until = store.get(key);
      if (until === undefined) return false;
      if (until > now()) return true;
      store.delete(key);
      return false;
    },
    add(key) {
      if (!store.has(key) && store.size >= maxKeys) {
        sweep();
        if (store.size >= maxKeys) return;
      }
      store.set(key, now() + ttlMs);
    },
    delete(key) { store.delete(key); },
    sweep,
  };
}
