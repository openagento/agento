// GitHub REST v3 (api-version 2022-11-28) + GraphQL v4 auth and safe fetch. Auth is a bearer PAT; the
// token comes from the resolved, already-decrypted scoped module config — it lives ONLY in the toolbox,
// never in the publisher and never in the agent.

// The API host is a CONSTANT, never config and never a request field: the toolbox is the one container
// holding secrets and is reachable from the agent's Docker network, so a caller-chosen origin would
// make it an SSRF probe. GitHub Enterprise Server is explicitly out of scope (see the plan).
const API_BASE = 'https://api.github.com';
const GRAPHQL_URL = 'https://api.github.com/graphql';
const API_VERSION = '2022-11-28';

const MAX_RETRIES = 3;              // 5xx-on-GET retries
const BACKOFF_BASE_MS = 500;
const BACKOFF_CAP_MS = 8000;

// Rate limits are governed by GitHub's own instruction, not by our backoff curve: "you should not
// retry your request until after that many seconds has elapsed … Otherwise, wait for at least one
// minute before retrying." So we wait exactly as instructed (never less), only ONCE, and only if the
// instructed wait is short enough to be worth holding the poll open — otherwise we give up and let
// the next cron tick retry. Retrying early is what gets an integration banned.
const RATE_LIMIT_MIN_WAIT_MS = 60000;
// The cap is bounded by the CALLER, not by politeness: the publisher reaches this code through
// `GitHubToolboxClient(timeout=60.0)` (Task 4), and httpx raises `ReadTimeout` when no response byte
// arrives within that read timeout. A 60s hold inside the request would therefore be answered into a
// socket the publisher had already abandoned — the poll fails AND the wait was spent. So: hold for at
// most 15s (plus the retried request, still far inside 60s) and otherwise return the rate-limit
// response so the NEXT cron tick retries — ~1m away on the changes lane, ~2h on comments. Note the
// consequence: an uninstructed rate limit (GitHub says "wait at least one minute") always exceeds the
// cap, so we never wait less than instructed and never hold the poll open for a minute either.
// Invariant, asserted by Task 9 test 13: RATE_LIMIT_MAX_WAIT_MS + a request's worth of slack must stay
// below `GitHubToolboxClient`'s default timeout. Changing either number without the other reintroduces
// exactly this bug.
const RATE_LIMIT_MAX_WAIT_MS = 15000;
const MAX_RATE_LIMIT_RETRIES = 1;

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// GitHub paginates with a Link header (there is no `next` body field as on Bitbucket).
export function linkNext(linkHeader) {
  return typeof linkHeader === 'string' && /(^|,)\s*<[^>]+>\s*;\s*rel="next"/.test(linkHeader);
}

// `deps` lets tests inject a fake fetch + a no-op sleep (so retry tests don't actually wait).
export function createGitHubAuth(cfg = {}, deps = {}) {
  const owner = cfg.github_owner || null;
  const token = cfg.github_token || null;
  // No `login` binding here: nothing in this file consumes it (the author filter lives in api-handlers.js and
  // github.js, each of which reads `cfg.github_login` itself), and `no-unused-vars` is configured to warn.

  const fetchImpl = deps.fetch || ((...a) => fetch(...a));
  const sleep = deps.sleep || defaultSleep;

  function isConfigured() {
    return !!(owner && token);
  }

  function baseHeaders(accept) {
    // Never log/echo the Authorization value.
    return {
      Authorization: `Bearer ${token}`,
      Accept: accept || 'application/vnd.github+json',
      'X-GitHub-Api-Version': API_VERSION,
    };
  }

  function buildQuery(query) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query || {})) {
      if (v !== undefined && v !== null) params.append(k, String(v));
    }
    const s = params.toString();
    return s ? `?${s}` : '';
  }

  function buildUrl(segments, query) {
    if (!Array.isArray(segments) || segments.length === 0) {
      throw new Error('ghFetch requires a non-empty path-segment array');
    }
    for (const seg of segments) {
      const s = String(seg);
      // Reject any attempt to smuggle an absolute URL through a segment (SSRF / scope-escape guard).
      if (/:\/\//.test(s) || /^https?:/i.test(s)) {
        throw new Error('ghFetch path segments must not be absolute URLs');
      }
    }
    // Repository sub-resources are `repos/{owner}/{repo}/...` — the owner is fixed by the scoped
    // config; reject a caller-supplied owner that differs from it.
    if (segments[0] === 'repos' && segments.length >= 2 && String(segments[1]) !== String(owner)) {
      throw new Error('ghFetch owner does not match the configured GitHub owner');
    }
    const path = segments.map((s) => encodeURIComponent(String(s))).join('/');
    return `${API_BASE}/${path}${buildQuery(query)}`;
  }

  const header = (res, k) =>
    (res.headers && typeof res.headers.get === 'function' ? res.headers.get(k) : null);

  // "Both primary and secondary rate limit violations return either 403 or 429" — a 403 is a rate
  // limit when it carries retry-after (secondary) or x-ratelimit-remaining: 0 (primary). A bare 403
  // is a permission error and must NOT be retried.
  function isRateLimited(res) {
    if (res.status === 429) return true;
    if (res.status !== 403) return false;
    return header(res, 'retry-after') !== null || header(res, 'x-ratelimit-remaining') === '0';
  }

  // Exactly the delay GitHub instructed — never shorter. Returns null when nothing was instructed.
  function instructedWaitMs(res) {
    const retryAfter = header(res, 'retry-after');
    if (retryAfter !== null) {
      const secs = parseInt(retryAfter, 10);
      if (Number.isFinite(secs) && secs >= 0) return secs * 1000;
    }
    if (header(res, 'x-ratelimit-remaining') === '0') {
      const reset = parseInt(header(res, 'x-ratelimit-reset'), 10);
      if (Number.isFinite(reset)) return Math.max(0, reset * 1000 - Date.now());
    }
    return null;
  }

  // segments: array of path segments (each encodeURIComponent'd).
  // opts: { query, method, body, headers, accept }.
  async function ghFetch(segments, opts = {}) {
    const { query = {}, method = 'GET', body, headers = {}, accept } = opts;
    const url = buildUrl(segments, query);
    const fetchOpts = { method, headers: { ...baseHeaders(accept), ...headers } };
    if (body !== undefined) {
      fetchOpts.body = typeof body === 'string' ? body : JSON.stringify(body);
      fetchOpts.headers['Content-Type'] = 'application/json';
    }

    let serverErrorAttempts = 0;
    let rateLimitAttempts = 0;
    while (true) {
      const res = await fetchImpl(url, fetchOpts);

      if (isRateLimited(res)) {
        // Rejected, not processed → safe to re-issue for any method, but only on GitHub's terms.
        const instructed = instructedWaitMs(res);
        const waitMs = instructed === null ? RATE_LIMIT_MIN_WAIT_MS : Math.max(instructed, 0);
        // Waiting longer than the cap would outlive the publisher's own HTTP timeout (see the constant);
        // shortening the wait is what GitHub bans integrations for. So: give up, let the next tick retry.
        if (waitMs > RATE_LIMIT_MAX_WAIT_MS || rateLimitAttempts >= MAX_RATE_LIMIT_RETRIES) return res;
        rateLimitAttempts += 1;
        await sleep(waitMs);
        continue;
      }

      // 5xx may have applied a mutating write before erroring → only retry idempotent GETs.
      if (res.status >= 500 && String(method).toUpperCase() === 'GET' && serverErrorAttempts < MAX_RETRIES) {
        await sleep(Math.min(BACKOFF_BASE_MS * 2 ** serverErrorAttempts, BACKOFF_CAP_MS));
        serverErrorAttempts += 1;
        continue;
      }

      return res;
    }
  }

  // Review-thread resolution state is not exposed by REST — it is GraphQL-only. Errors are sanitized:
  // only GraphQL `message` fields are surfaced, never the request (which carries the token).
  async function ghGraphql(query, variables = {}) {
    const res = await fetchImpl(GRAPHQL_URL, {
      method: 'POST',
      headers: { ...baseHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, variables }),
    });
    if (!res.ok) {
      await res.text().catch(() => '');
      throw new Error(`GraphQL HTTP ${res.status}`);
    }
    const data = await res.json();
    if (Array.isArray(data.errors) && data.errors.length > 0) {
      throw new Error(`GraphQL error: ${data.errors.map((e) => e.message).join('; ')}`);
    }
    return data.data;
  }

  return {
    isConfigured,
    ghFetch,
    ghGraphql,
  };
}
