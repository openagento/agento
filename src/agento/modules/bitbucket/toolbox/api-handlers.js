import { createBitbucketAuth } from './bitbucket-auth.js';

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
    const slug = raw.trim();
    if (slug && !seen.has(slug)) {
      seen.add(slug);
      out.push(slug);
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
  return Math.min(Math.max(Math.min(...candidates), 1), 50);
}

// Follow `page`-based pagination up to bounded caps (never an unbounded crawl). Returns collected
// `values`. Throws a sanitized Error (status only) on a non-2xx page.
//
// `noPageParam`: omit the `page` key entirely. Bitbucket Cloud's PR /commits endpoint rejects
// page-number pagination with HTTP 400 {"error":{"message":"Invalid page"}} — it accepts only
// `pagelen` and exposes a `next` cursor. Callers hitting such endpoints pass this and cap maxPages:1.
async function collectPages(auth, segments, baseQuery, { maxItems = Infinity, maxPages = 5, noPageParam = false } = {}) {
  const out = [];
  for (let page = 1; page <= maxPages; page += 1) {
    const query = noPageParam ? { ...baseQuery, pagelen: 50 } : { ...baseQuery, page, pagelen: 50 };
    const res = await auth.bbFetch(segments, { query });
    if (!res.ok) {
      await res.text().catch(() => ''); // drain, discard (never surface provider body)
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    const values = Array.isArray(data.values) ? data.values : [];
    for (const v of values) {
      out.push(v);
      if (out.length >= maxItems) return out;
    }
    if (values.length < 50 || !data.next) break;
  }
  return out;
}

// --- POST /api/bitbucket/verify (transient body creds; the ONE endpoint that uses body creds, run
//     before anything is saved). Verifies against GET /2.0/user and returns the account_uuid. ---
export function createVerifyHandler(log, authFactory = createBitbucketAuth) {
  return async (req, res) => {
    const { workspace, email, api_token: apiToken } = req.body || {};
    const auth = authFactory({
      bitbucket_workspace: workspace,
      bitbucket_email: email,
      bitbucket_api_token: apiToken,
    });
    if (!auth.isConfigured()) {
      return res.json({ ok: false, status: 400, detail: 'workspace, email and api_token are all required' });
    }
    try {
      const r = await auth.bbFetch(['user']);
      if (!r.ok) {
        await r.text().catch(() => '');
        log('api/bitbucket/verify', 'ERROR', `verify failed (HTTP ${r.status})`);
        return res.json({ ok: false, status: r.status, detail: `Bitbucket auth failed (HTTP ${r.status})` });
      }
      const user = await r.json();
      log('api/bitbucket/verify', 'OK', `verified ${user.nickname || user.display_name || ''}`);
      return res.json({
        ok: true,
        account_uuid: user.uuid,
        username: user.nickname || user.display_name || user.account_id,
      });
    } catch (err) {
      log('api/bitbucket/verify', 'ERROR', err.message);
      return res.status(500).json({ ok: false, status: 500, detail: 'Internal error during verification' });
    }
  };
}

// --- POST /api/bitbucket/open-prs (publisher). Scoped-config is the authorization boundary; body
//     values may only NARROW (top), never authorize (no caller workspace/uuid/allowlist). ---
export function createOpenPrsHandler({ loadScopedDbOverrides, loadModuleConfigs }, log, authFactory = createBitbucketAuth) {
  return async (req, res) => {
    const { agent_view_id: agentViewId, lane, top } = req.body || {};
    if (!agentViewId) {
      return res.status(400).json({ error: 'agent_view_id is required' });
    }

    // Fail closed on an unknown agent_view: the base loader returns global overrides with
    // agentViewMeta=null when the view does not exist — never silently act at global scope.
    const { overrides, agentViewMeta } = await loadScopedDbOverrides(agentViewId);
    if (!agentViewMeta) {
      log('api/bitbucket/open-prs', 'ERROR', `unknown agent_view_id=${agentViewId}`);
      return res.status(404).json({ error: 'Unknown agent_view_id' });
    }

    const moduleConfigs = await loadModuleConfigs(overrides);
    const cfg = moduleConfigs.bitbucket || {};

    if (!isEnabled(cfg.enabled)) {
      return res.status(403).json({ error: 'Bitbucket channel disabled for this scope' });
    }
    const workspace = cfg.bitbucket_workspace;
    const accountUuid = cfg.bitbucket_account_uuid;
    if (!workspace || !accountUuid || !cfg.bitbucket_email || !cfg.bitbucket_api_token) {
      return res.status(403).json({ error: 'Bitbucket not fully configured for this scope' });
    }

    const repos = parseRepoAllowlist(cfg.repo_allowlist);
    if (repos.length === 0) {
      return res.json({ pull_requests: [], errors: [] }); // empty allow-list ⇒ nothing to scan
    }
    const effectiveTop = clampTop(top, cfg.poll_top);
    const auth = authFactory(cfg);

    const pullRequests = [];
    const errors = [];

    for (const repo of repos) {
      try {
        const q = `author.uuid="${accountUuid}" AND state="OPEN"`;
        const prs = await collectPages(
          auth,
          ['repositories', workspace, repo, 'pullrequests'],
          { q },
          { maxItems: effectiveTop, maxPages: 10 },
        );
        for (const pr of prs) {
          const base = { workspace, repo, id: pr.id, title: pr.title, updated_on: pr.updated_on };
          if (lane === 'comments') {
            pullRequests.push({ ...base, ...(await buildCommentsRecord(auth, workspace, repo, pr.id)) });
          } else if (lane === 'changes') {
            pullRequests.push({ ...base, ...(await buildChangesRecord(auth, workspace, repo, pr, accountUuid)) });
          } else {
            pullRequests.push(base);
          }
        }
      } catch (err) {
        // Per-repo isolation: a failing repo is reported, the rest still return.
        log('api/bitbucket/open-prs', 'ERROR', `repo=${repo} ${err.message}`);
        errors.push({ repo, error: err.message });
      }
    }

    log('api/bitbucket/open-prs', 'OK', `lane=${lane} prs=${pullRequests.length} errors=${errors.length}`);
    return res.json({ pull_requests: pullRequests, errors });
  };
}

async function buildCommentsRecord(auth, workspace, repo, prId) {
  const rawComments = await collectPages(
    auth,
    ['repositories', workspace, repo, 'pullrequests', prId, 'comments'],
    { sort: '-created_on' }, // best-effort hint only; Python computes order-independently
    { maxPages: 5 },
  );
  const comments = rawComments.map((c) => ({
    id: c.id,
    author_uuid: c.user && c.user.uuid,
    created_on: c.created_on,
    deleted: !!c.deleted,
    resolved: !!c.resolution,
  }));
  const rawCommits = await collectPages(
    auth,
    ['repositories', workspace, repo, 'pullrequests', prId, 'commits'],
    {},
    { maxPages: 1, maxItems: 50, noPageParam: true }, // /commits rejects `page` with HTTP 400
  );
  const commits = rawCommits.map((cm) => ({ date: cm.date }));
  return { comments, commits };
}

async function buildChangesRecord(auth, workspace, repo, pr, accountUuid) {
  // Cheap pre-filter: if the list PR object carries participants and none (other than the agent) shows
  // changes_requested, skip the /activity fetch. When participants is absent we don't assume — we fetch.
  if (Array.isArray(pr.participants)) {
    const anyChanges = pr.participants.some(
      (p) => p && p.state === 'changes_requested' && p.user && p.user.uuid !== accountUuid,
    );
    if (!anyChanges) return { changes_requests: [] };
  }
  const activity = await collectPages(
    auth,
    ['repositories', workspace, repo, 'pullrequests', pr.id, 'activity'],
    {},
    { maxPages: 5 },
  );
  const changesRequests = [];
  for (const entry of activity) {
    const cr = entry && entry.changes_requested;
    if (!cr) continue;
    const userUuid = cr.user && cr.user.uuid;
    if (userUuid === accountUuid) continue; // agent's own ignored
    changesRequests.push({ user_uuid: userUuid, date: cr.date });
  }
  return { changes_requests: changesRequests };
}
