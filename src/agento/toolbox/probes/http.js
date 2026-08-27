// The `http` config-test kind — one request, one expected status.
//
// Two properties this probe must have and a bare `curl` does not:
//   * `expect` is explicit. `curl` exits 0 on a 401, so the shell version of
//     this test reported success on a dead token.
//   * redirects are not followed. `fetch` follows them by default, which would
//     re-send the Authorization header to whatever host the 30x names.
export const required = ['url'];

const ALLOWED_SCHEMES = new Set(['http:', 'https:']);

export async function probe(spec, { timeoutMs = 15_000, deps = {} } = {}) {
  const doFetch = deps.fetch || globalThis.fetch;
  const raw = String(spec.url ?? '').trim();

  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    // An interpolated `{module/host}` that resolved to something empty leaves a
    // relative URL behind. That is an unconfigured integration, not a bad one.
    if (raw.startsWith('/') || raw === '') {
      return { status: 'not_configured', code: 'NOT_SET', detail: 'no host configured' };
    }
    return { status: 'error', code: 'BAD_URL', detail: 'the test URL is not a valid absolute URL' };
  }
  if (!ALLOWED_SCHEMES.has(parsed.protocol)) {
    return { status: 'error', code: 'BAD_URL', detail: `unsupported scheme '${parsed.protocol}'` };
  }
  if (parsed.username || parsed.password) {
    // Credentials in the URL end up in every message this probe prints.
    return { status: 'error', code: 'BAD_URL', detail: 'the test URL must not embed credentials' };
  }

  const headers = { ...(spec.headers || {}) };
  if (Array.isArray(spec.basic) && spec.basic.length === 2) {
    const [user, secret] = spec.basic.map((v) => String(v ?? ''));
    headers.Authorization = `Basic ${Buffer.from(`${user}:${secret}`).toString('base64')}`;
  } else if (spec.bearer) {
    headers.Authorization = `Bearer ${String(spec.bearer)}`;
  }

  const expected = parseInt(spec.expect ?? 200, 10) || 200;
  const controller = new globalThis.AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const started = Date.now();
  try {
    const res = await doFetch(parsed.toString(), {
      method: String(spec.method || 'GET').toUpperCase(),
      headers,
      // 'manual', not 'error': with 'error' a real `fetch` REJECTS, so a login-page
      // 30x would surface as `ERROR [UNREACHABLE]` — "could not check" — when the
      // truth is a definite `HTTP_302`. 'manual' does not follow the redirect
      // either, so the Authorization header is never re-sent to another host;
      // the status is simply returned and compared like any other.
      redirect: 'manual',
      signal: controller.signal,
    });
    const ms = Date.now() - started;
    if (res.status === expected) return { status: 'ok', code: 'OK', ms };
    return {
      status: 'fail',
      code: `HTTP_${res.status}`,
      detail: `expected HTTP ${expected}, got ${res.status} ${res.statusText || ''}`.trim(),
      ms,
    };
  } catch (err) {
    // Nothing was authenticated, so this is not a verdict on the credential.
    const aborted = err && (err.name === 'AbortError' || err.code === 'ABORT_ERR');
    return {
      status: 'error',
      code: aborted ? 'PROBE_TIMEOUT' : 'UNREACHABLE',
      detail: aborted
        ? `no answer within ${timeoutMs} ms`
        : `request failed (${(err && err.name) || 'Error'})`,
      ms: Date.now() - started,
    };
  } finally {
    clearTimeout(timer);
  }
}
