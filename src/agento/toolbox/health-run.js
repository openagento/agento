const HEALTHCHECK_TIMEOUT_MS = 10_000;

// Stable categories. A healthcheck error routinely embeds the credential it just used
// (a connection string, an SMTP reply, a Graph error naming the client secret), so the raw
// text is inspected ONLY to pick a category and is then discarded — never returned, never
// logged. An operator who needs the driver text reads it from the backend's own log.
export const HEALTH_ERROR_CATEGORIES = Object.freeze([
  'timeout',
  'auth failed',
  'unreachable',
  'misconfigured',
  'failed',
]);

export function sanitizeHealthError(raw) {
  const text = typeof raw === 'string' ? raw : String(raw?.message ?? raw ?? '');
  // Idempotent: an adapter that already picked a category hands it straight back. Without
  // this the central backstop would re-classify its own output — 'unreachable' matches none
  // of the patterns below and would degrade to 'failed' on the second pass.
  if (HEALTH_ERROR_CATEGORIES.includes(text)) return text;
  if (/timeout|timed out|ETIMEDOUT|abort/i.test(text)) return 'timeout';
  // Transport evidence OUTRANKS vocabulary. 'auth' matches broad words — login, credential,
  // token — that appear in ordinary HOSTNAMES and service names, so a plain
  // `ENOTFOUND login.internal` would read as a credential problem and send an operator after
  // the wrong thing. A syscall code or a 5xx is evidence about the connection itself.
  if (/ECONNREFUSED|ENOTFOUND|EHOSTUNREACH|ECONNRESET|EPIPE|EAI_AGAIN|socket|network|dns|HTTP 5\d\d/i.test(text)) {
    return 'unreachable';
  }
  if (/\b(401|403)\b|unauthor|forbidden|auth|AADSTS|credential|secret|password|denied|token|login/i.test(text)) {
    return 'auth failed';
  }
  if (/not configured|missing|invalid|unknown tool|ENOENT|config/i.test(text)) return 'misconfigured';
  return 'failed';
}

function sanitizeCheck(check) {
  if (!check || typeof check !== 'object') {
    return { tool: 'unknown', status: 'fail', error: 'failed' };
  }
  if (check.error === undefined || check.error === null) return check;
  return { ...check, error: sanitizeHealthError(check.error) };
}

// The ONE place every registered healthcheck's result — fulfilled and rejected — passes
// through. Sanitizing here rather than in each adapter is what makes the guarantee hold for
// an adapter written next month: there is no per-implementation opt-in to forget.
export async function runHealthchecks(healthchecks) {
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
      for (const check of result.value) checks.push(sanitizeCheck(check));
    } else {
      checks.push({ tool: 'unknown', status: 'fail', error: sanitizeHealthError(result.reason) });
    }
  }
  return checks;
}
