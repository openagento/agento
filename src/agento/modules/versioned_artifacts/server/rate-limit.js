// Fixed-window counters for the artifacts server (RULES.md SEC-12). Plain `node:http`
// here, so not the toolbox's express-rate-limit. Memory is bounded: an expired entry is
// dropped when read, `sweep()` drops every expired one, and a store holds at most
// `maxKeys` — at the cap, after a sweep, a new key is refused by `allowed()` and `take()`
// alike (fail closed under a flood), so a caller refuses it before doing any work.
export const WINDOW_MS = 60_000;
export const AUTH_FAILURES_PER_ADDRESS = 60;
export const REQUESTS_PER_CREDENTIAL = 600;
export const MAX_KEYS = 10_000;

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
    /** Is `key` under its limit, with room to count it? Counts nothing. */
    allowed(key) {
      const e = live(key);
      return e ? e.count < limit : !full();
    },
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
    sweep,
    size: () => store.size,
  };
}
