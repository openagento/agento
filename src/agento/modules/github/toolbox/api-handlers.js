import { createGitHubAuth, linkNext } from './github-auth.js';
// Imported and re-exported so Task 7 and the tests have ONE definition of the key list (see env-guard.js).
import { VIEW_SCOPED_ENV_KEYS, offendingEnvKeys } from './env-guard.js';
export { VIEW_SCOPED_ENV_KEYS, offendingEnvKeys };

// The ENV guard, as one helper both handlers call FIRST — before the body is parsed and before any
// config is resolved, because `resolveModuleField` would otherwise hand every `agent_view_id` the same
// ENV token (`config-loader.js:233`). Returns true when it has already answered the request.
function refusedForEnvOverride(res, log, route) {
  const offenders = offendingEnvKeys();
  if (!offenders.length) return false;
  log(route, 'ERROR', `refusing: view-scoped fields set as global ENV overrides: ${offenders.join(', ')}`);
  res.status(503).json({
    error: `github: view-scoped fields are set as global ENV overrides: ${offenders.join(', ')}`,
  });
  return true;
}

// No `CHANGES_REQUESTED` constant lives here. This handler emits the FULL review history with the
// state strings verbatim; the "is the request still outstanding" decision is Python's
// (`review_scan.detect_changes_requested`, which owns the constant). A second copy on the JS side
// would have no consumer — and an unused constant is how the two sides silently drift apart.
export const LANES = ['comments', 'changes'];

const PER_PAGE = 100; // GitHub's maximum

// --- config normalization (resolved module config arrives as strings or config.json natives) ---

export function isEnabled(value) {
  if (value === true) return true;
  if (typeof value === 'string') return value === '1' || value.toLowerCase() === 'true';
  return false;
}

export function parseRepoAllowlist(value) {
  const seen = new Set();
  const out = [];
  for (const raw of String(value || '').split(',')) {
    const name = raw.trim();
    if (name && !seen.has(name)) {
      seen.add(name);
      out.push(name);
    }
  }
  return out;
}

function clampTop(top, pollTop) {
  const candidates = [];
  const t = parseInt(top, 10);
  if (Number.isFinite(t)) candidates.push(t);
  const p = parseInt(pollTop, 10);
  candidates.push(Number.isFinite(p) ? p : 20);
  return Math.min(Math.max(Math.min(...candidates), 1), 100);
}

// Follow GitHub's Link-header pagination up to bounded caps (never an unbounded crawl). `keep` filters
// each item client-side BEFORE the maxItems cap, so a cap on agent-authored PRs is not consumed by
// third-party PRs (G-5). Throws a sanitized Error (status only) on a non-2xx page.
//
// Returns { items, truncated }. `truncated` is true when the scan stopped at `maxPages` while GitHub
// still advertised a next page — i.e. the result is INCOMPLETE. Every caller must propagate that: a
// bounded scan that looks identical to a complete one is how a publisher silently makes decisions on
// partial data (G-18). Reaching `maxItems` is NOT truncation — that cap is the requested amount.
// Exported: Task 7's read tools (`github_get_pr_comments`, `github_get_pr_reviews`) page the same
// collections with the same caps and the same truncation contract. One paginator, not two.
export async function collectPages(auth, segments, baseQuery, { maxItems = Infinity, maxPages = 5, keep = () => true } = {}) {
  const items = [];
  for (let page = 1; page <= maxPages; page += 1) {
    const res = await auth.ghFetch(segments, { query: { ...baseQuery, page, per_page: PER_PAGE } });
    if (!res.ok) {
      await res.text().catch(() => ''); // drain, discard (never surface provider body)
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    const values = Array.isArray(data) ? data : [];
    for (const v of values) {
      if (!keep(v)) continue;
      items.push(v);
      if (items.length >= maxItems) return { items, truncated: false };
    }
    const link = res.headers && typeof res.headers.get === 'function' ? res.headers.get('link') : null;
    // The Link header is GitHub's pagination contract and is authoritative on its own: follow `rel="next"`
    // whenever it is present. A short page that still advertises a next page is exactly the case an
    // item-count heuristic gets wrong — it stops early and reports complete, which is worse than any
    // truncation because nothing marks the scan incomplete.
    const hasNext = linkNext(link);
    if (!hasNext) return { items, truncated: false };
    if (page === maxPages) return { items, truncated: true };
  }
  return { items, truncated: false };
}

// Records an incomplete scan on both the PR record (so the publisher can see it) and the response
// `errors[]` (so the operator sees it in the publisher log).
// `reason` defaults to the page-cap case; pass one for a scan that failed some other way (e.g. the head
// commit could not be read). Every caller must go through here: an `errors[]` entry alone is invisible
// to `lane_data_is_complete`, so "logged it" and "did not publish from it" must be one action.
function noteTruncation(record, errors, repo, scan, log, reason = 'hit the page cap') {
  (record.truncated ||= []).push(scan);
  errors.push({ repo, error: `incomplete scan: ${scan} ${reason}` });
  log('api/github/open-prs', 'WARN', `truncated ${scan} for repo=${repo} (${reason})`);
}

// --- POST /api/github/verify (transient body creds; the ONE endpoint that uses body creds, run before
//     anything is saved). Verifies against GET /user and returns login + numeric id. ---
export function createVerifyHandler(log, authFactory = createGitHubAuth) {
  return async (req, res) => {
    if (refusedForEnvOverride(res, log, 'api/github/verify')) return;
    const { token } = req.body || {};
    // The GitHub host is NOT a request field (see github-auth.js): a caller-chosen origin would turn
    // this endpoint into an SSRF probe against the internal network, using the toolbox's egress.
    if (typeof token !== 'string' || token.trim() === '') {
      return res.json({ ok: false, status: 400, detail: 'token is required' });
    }
    // `owner` is irrelevant to GET /user; pass a placeholder so isConfigured() is satisfied.
    const auth = authFactory({ github_owner: '_', github_token: token });
    try {
      const r = await auth.ghFetch(['user']);
      if (!r.ok) {
        await r.text().catch(() => '');
        log('api/github/verify', 'ERROR', `verify failed (HTTP ${r.status})`);
        return res.json({ ok: false, status: r.status, detail: `GitHub auth failed (HTTP ${r.status})` });
      }
      const user = await r.json();
      log('api/github/verify', 'OK', `verified ${user.login || ''}`);
      return res.json({ ok: true, login: user.login, id: user.id });
    } catch (err) {
      log('api/github/verify', 'ERROR', err.message);
      return res.status(500).json({ ok: false, status: 500, detail: 'Internal error during verification' });
    }
  };
}

// --- POST /api/github/open-prs (publisher). Scoped-config is the authorization boundary; body values
//     may only NARROW (top), never authorize (no caller owner/login/allowlist). ---
export function createOpenPrsHandler({ loadScopedDbOverrides, loadModuleConfigs }, log, authFactory = createGitHubAuth) {
  return async (req, res) => {
    if (refusedForEnvOverride(res, log, 'api/github/open-prs')) return;
    const { agent_view_id: agentViewId, lane, top } = req.body || {};
    const viewId = Number(agentViewId);
    if (!Number.isInteger(viewId) || viewId <= 0) {
      return res.status(400).json({ error: 'agent_view_id must be a positive integer' });
    }
    // A typo'd lane must fail loudly: silently returning bare records would make the publisher find
    // no work forever, which looks exactly like "nothing to do".
    if (!LANES.includes(lane)) {
      return res.status(400).json({ error: `lane must be one of: ${LANES.join(', ')}` });
    }

    // Fail closed on an unknown agent_view: the base loader returns global overrides with
    // agentViewMeta=null when the view does not exist — never silently act at global scope.
    const { overrides, agentViewMeta } = await loadScopedDbOverrides(viewId);
    if (!agentViewMeta) {
      log('api/github/open-prs', 'ERROR', `unknown agent_view_id=${viewId}`);
      return res.status(404).json({ error: 'Unknown agent_view_id' });
    }

    const moduleConfigs = await loadModuleConfigs(overrides);
    const cfg = moduleConfigs.github || {};

    if (!isEnabled(cfg.enabled)) {
      return res.status(403).json({ error: 'GitHub channel disabled for this scope' });
    }
    const owner = cfg.github_owner;
    const login = cfg.github_login;
    if (!owner || !login || !cfg.github_token) {
      return res.status(403).json({ error: 'GitHub not fully configured for this scope' });
    }

    const repos = parseRepoAllowlist(cfg.repo_allowlist);
    if (repos.length === 0) {
      return res.json({ pull_requests: [], errors: [] }); // empty allow-list ⇒ nothing to scan
    }
    const effectiveTop = clampTop(top, cfg.poll_top);
    const auth = authFactory(cfg);
    const isAgent = (l) => typeof l === 'string' && l.toLowerCase() === String(login).toLowerCase();

    const pullRequests = [];
    const errors = [];

    for (const repo of repos) {
      try {
        // GitHub's list-pulls has NO author filter, so we filter client-side and cap on MATCHING PRs.
        const { items: prs, truncated } = await collectPages(
          auth,
          ['repos', owner, repo, 'pulls'],
          { state: 'open', sort: 'updated', direction: 'desc' },
          { maxItems: effectiveTop, maxPages: 10, keep: (pr) => isAgent(pr.user && pr.user.login) },
        );
        if (truncated) {
          errors.push({ repo, error: 'incomplete scan: open-PR discovery hit the page cap' });
          log('api/github/open-prs', 'WARN', `truncated PR discovery for repo=${repo}`);
        }
        for (const pr of prs) {
          const record = { owner, repo, id: pr.number, title: pr.title, updated_at: pr.updated_at };
          if (lane === 'comments') {
            await addCommentsRecord(auth, owner, repo, pr, record, errors, log);
          } else {
            await addChangesRecord(auth, owner, repo, pr.number, record, errors, log, isAgent);
          }
          pullRequests.push(record);
        }
      } catch (err) {
        // Per-repo isolation: a failing repo is reported, the rest still return.
        log('api/github/open-prs', 'ERROR', `repo=${repo} ${err.message}`);
        errors.push({ repo, error: err.message });
      }
    }

    log('api/github/open-prs', 'OK', `lane=${lane} prs=${pullRequests.length} errors=${errors.length}`);
    return res.json({ pull_requests: pullRequests, errors });
  };
}

// GraphQL is the ONLY way to learn whether an inline review thread is resolved (REST exposes neither
// thread ids nor resolution state). Returns a Set of resolved review-comment ids (fullDatabaseId).
// `pageInfo.hasNextPage` is requested at BOTH levels so the query's own bound is DETECTABLE — an
// undetectable bound is exactly the silent-truncation shape G-18 bans, even when its failure
// direction is safe.
export const REVIEW_THREADS_QUERY = `
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:100){
        pageInfo{ hasNextPage }
        nodes{
          id isResolved
          comments(first:100){ pageInfo{ hasNextPage } nodes{ fullDatabaseId } }
        }
      }
    }
  }
}`;

// Returns { resolved: Set<fullDatabaseId>, truncated: boolean }.
async function fetchResolvedCommentIds(auth, owner, repo, number) {
  const data = await auth.ghGraphql(REVIEW_THREADS_QUERY, { owner, name: repo, number: Number(number) });
  const threads = data?.repository?.pullRequest?.reviewThreads;
  const nodes = threads?.nodes || [];
  let truncated = !!threads?.pageInfo?.hasNextPage;
  const resolved = new Set();
  for (const thread of nodes) {
    if (thread?.comments?.pageInfo?.hasNextPage) truncated = true;
    if (!thread || !thread.isResolved) continue;
    for (const c of thread.comments?.nodes || []) {
      // OV-2: the query selects `fullDatabaseId` ONLY. Its predecessor `databaseId` (Int) is
      // deprecated, and selecting a removed field fails the WHOLE query at validation time — so naming
      // both would not be a fallback, it would be the outage. `Number()` normalizes to the shape this
      // Set is compared against — the REST comment's `id`, which arrived through JSON.parse and is
      // ALREADY a double. The match is therefore exact only up to 2^53 (GitHub's comment ids are ~10
      // digits); nothing here can restore precision the REST side already lost.
      const id = c?.fullDatabaseId;
      if (id !== undefined && id !== null && id !== '') resolved.add(Number(id));
    }
  }
  return { resolved, truncated };
}

// GitHub splits PR feedback across THREE surfaces; the publisher's watermark logic needs them merged.
// Mutates `record` in place so truncation flags from several scans accumulate on one object.
async function addCommentsRecord(auth, owner, repo, pr, record, errors, log) {
  const number = pr.number;
  const scans = [
    ['issue_comments', ['repos', owner, repo, 'issues', number, 'comments']],
    ['review_comments', ['repos', owner, repo, 'pulls', number, 'comments']],
    ['reviews', ['repos', owner, repo, 'pulls', number, 'reviews']],
  ];
  const collected = {};
  for (const [name, segments] of scans) {
    const { items, truncated } = await collectPages(auth, segments, {}, { maxPages: 5 });
    collected[name] = items;
    if (truncated) noteTruncation(record, errors, repo, name, log);
  }

  // Resolution is best-effort at the REPO level only: a GraphQL failure must not lose the other PRs.
  // For THIS PR it means the resolution state is unknown, and unknown is not "unresolved" — the record is
  // marked incomplete (`truncated: ['review_comments']`) and the publisher skips its comments lane this
  // poll. The same applies to the query's own first:100 bound, which is why it is not paginated but IS
  // detected via `pageInfo.hasNextPage`.
  let resolvedIds = new Set();
  try {
    const { resolved, truncated } = await fetchResolvedCommentIds(auth, owner, repo, number);
    resolvedIds = resolved;
    // Over 100 threads (or 100 comments in one thread): some resolved threads are invisible to us,
    // so the comments lane's verdict is not trustworthy for this PR.
    if (truncated) noteTruncation(record, errors, repo, 'review_comments', log);
  } catch (err) {
    // Resolution state unknown ⇒ mark the scan incomplete and skip this PR's comments lane (the same
    // class of unknown as a capped scan). NOT "degrade to unresolved and do the work": that would
    // re-raise feedback the agent already resolved, every poll, forever.
    noteTruncation(record, errors, repo, 'review_comments', log,
      `thread-resolution state unavailable for #${number}: ${err.message}`);
  }

  // A null `user` is a deleted/ghost account: drop it here so the publisher never mints an
  // ACCOUNT-trust requester for an unidentifiable author (G-17).
  const comments = [];
  const push = (login, createdAt, id, surface, resolved) => {
    if (!login) return;
    comments.push({ id, author_login: login, created_at: createdAt, surface, resolved });
  };
  for (const c of collected.issue_comments) push(c.user && c.user.login, c.created_at, c.id, 'issue', false);
  for (const c of collected.review_comments) {
    push(c.user && c.user.login, c.created_at, c.id, 'review', resolvedIds.has(c.id));
  }
  for (const r of collected.reviews) {
    if (!r.body || !String(r.body).trim()) continue; // a bodiless APPROVE/COMMENT carries no feedback
    push(r.user && r.user.login, r.submitted_at, r.id, 'review_body', false);
  }

  // The watermark needs the NEWEST commit. Paging the PR's commit list would silently give the wrong
  // answer past 100 commits, so fetch the head commit directly — one request, always correct (G-18).
  record.comments = comments;
  record.commits = [];
  // An absent watermark is NOT a safe fallback: `flag_unanswered` would then fall back to the agent's
  // last comment and re-raise feedback a force-push already answered. All three failure shapes below —
  // no head SHA on the list payload, a failed/unparseable fetch, a commit with no usable date — mark
  // `head_commit` truncated, which is a comments-lane required scan (Task 2), so that PR's comments lane
  // is skipped this poll and re-tried on the next one.
  const headSha = pr.head && pr.head.sha;
  if (!headSha) {
    noteTruncation(record, errors, repo, 'head_commit', log, `absent on the list payload for #${number}`);
  } else {
    try {
      const r = await auth.ghFetch(['repos', owner, repo, 'commits', headSha]);
      if (!r.ok) {
        await r.text().catch(() => '');
        throw new Error(`HTTP ${r.status}`);
      }
      const commit = await r.json();
      const date = commit.commit?.committer?.date || commit.commit?.author?.date;
      if (date) {
        record.commits = [{ date }];
      } else {
        noteTruncation(record, errors, repo, 'head_commit', log, `carried no timestamp for #${number}`);
      }
    } catch (err) {
      noteTruncation(record, errors, repo, 'head_commit', log, `unavailable for #${number}: ${err.message}`);
    }
  }
}

async function addChangesRecord(auth, owner, repo, number, record, errors, log, isAgent) {
  // Unlike Bitbucket there is no participants pre-filter on the list payload, so every open agent PR
  // costs one /reviews fetch in this lane (bounded by poll_top).
  const { items: reviews, truncated } = await collectPages(
    auth, ['repos', owner, repo, 'pulls', number, 'reviews'], {}, { maxPages: 5 },
  );
  if (truncated) noteTruncation(record, errors, repo, 'reviews', log);
  // Emit the FULL non-agent history (state included) — a CHANGES_REQUESTED review is not removed when
  // the same reviewer later approves, so only the pure layer, seeing every review, can tell whether
  // the request is still outstanding (G-16).
  record.reviews = reviews
    .filter((r) => r && r.user && r.user.login && !isAgent(r.user.login))
    // `id` is not decoration: it is what makes the publisher's changes-lane idempotency key unique when
    // two reviews share a submitted_at second (Task 2 `build_changes_key`).
    .map((r) => ({ id: r.id, user_login: r.user.login, date: r.submitted_at, state: r.state }));
}
