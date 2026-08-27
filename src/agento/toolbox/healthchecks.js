import { redactSecrets } from './config-loader.js';

export const HEALTHCHECK_TIMEOUT_MS = 10_000;

/**
 * Run every registered healthcheck in parallel and return their `checks[]`.
 *
 * `obscureValues` are masked in every `error` string — in BOTH branches. The
 * rejected branch matters as much as the fulfilled one: a healthcheck that
 * throws puts the exception message in the body.
 */
export async function runHealthchecks(healthchecks, obscureValues = []) {
  const results = await Promise.allSettled(
    healthchecks.map(fn => {
      const controller = new globalThis.AbortController();
      let timer;
      const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new Error('timeout'));
        }, HEALTHCHECK_TIMEOUT_MS);
      });
      return Promise.race([
        fn({ signal: controller.signal, timeoutMs: HEALTHCHECK_TIMEOUT_MS }),
        timeout,
      ]).finally(() => clearTimeout(timer));
    })
  );

  const checks = [];
  for (const result of results) {
    if (result.status === 'fulfilled') {
      for (const c of result.value || []) {
        checks.push(
          c && typeof c.error === 'string'
            ? { ...c, error: redactSecrets(c.error, obscureValues) }
            : c
        );
      }
    } else {
      checks.push({
        tool: 'unknown',
        status: 'fail',
        error: redactSecrets(result.reason?.message || 'unknown error', obscureValues),
      });
    }
  }
  return checks;
}
