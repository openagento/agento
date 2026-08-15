import { describe, it, expect, vi } from 'vitest';

import { register } from '../../modules/bitbucket/toolbox/bitbucket.js';

function makeServer() {
  const tools = {};
  return { tools, tool(name, desc, schema, handler) { tools[name] = { desc, schema, handler }; } };
}

function jsonRes(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body, text: async () => '' };
}

function fakeAuth(handler) {
  return { isConfigured: () => true, bbFetch: vi.fn(async (segments, opts = {}) => handler(segments, opts)) };
}

const CFG = {
  bitbucket_workspace: 'acme', bitbucket_email: 'e@x.com',
  bitbucket_api_token: 'tok', repo_allowlist: 'api,web',
};

function ctx(auth, overrides = {}) {
  return {
    log: vi.fn(),
    moduleConfigs: { bitbucket: CFG },
    isToolEnabled: () => true,
    bitbucketAuthFactory: () => auth,
    ...overrides,
  };
}

const ALL_TOOLS = [
  'bitbucket_add_comment', 'bitbucket_create_pr', 'bitbucket_get_pr',
  'bitbucket_get_pr_activity', 'bitbucket_get_pr_comments', 'bitbucket_get_pr_diff',
  'bitbucket_resolve_comment', 'bitbucket_set_review',
];

describe('bitbucket tools: registration + opt-in gating', () => {
  it('registers all 8 tools when enabled', () => {
    const s = makeServer();
    register(s, ctx(fakeAuth(() => jsonRes({}))));
    expect(Object.keys(s.tools).sort()).toEqual(ALL_TOOLS);
  });

  it('skips a tool whose is_enabled resolves false', () => {
    const s = makeServer();
    register(s, ctx(fakeAuth(() => jsonRes({})), { isToolEnabled: (n) => n !== 'bitbucket_create_pr' }));
    expect(s.tools.bitbucket_create_pr).toBeUndefined();
    expect(s.tools.bitbucket_get_pr).toBeDefined();
  });
});

describe('bitbucket tools: allow-list enforcement + workspace fixed by config', () => {
  it('no tool declares a workspace parameter (workspace is config-only, never caller-supplied)', () => {
    const s = makeServer();
    register(s, ctx(fakeAuth(() => jsonRes({}))));
    for (const name of ALL_TOOLS) {
      expect(s.tools[name].schema).not.toHaveProperty('workspace');
    }
  });

  it('rejects a repo outside the allow-list, without calling Bitbucket', async () => {
    const auth = fakeAuth(() => jsonRes({}));
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_get_pr.handler({ repo: 'secret', pr_id: 1 });
    expect(r.isError).toBe(true);
    expect(auth.bbFetch).not.toHaveBeenCalled();
  });

  // Every tool states the format on the ARGUMENT, not only in the tool's prose — a model that knows the
  // "workspace/repo" spelling from the Bitbucket UI writes it unless the schema says otherwise.
  it.each(ALL_TOOLS)('%s declares the bare-slug rule on the repo argument itself', (name) => {
    const s = makeServer();
    register(s, ctx(fakeAuth(() => jsonRes({}))));
    expect(s.tools[name].schema.repo.description).toMatch(/bare repository slug WITHOUT the workspace prefix/i);
  });

  // The ARGUMENT is normalized, the ALLOW-LIST is not (next test). Stripping a prefix that equals the
  // configured workspace cannot redirect the call — the URL is built from config either way.
  it('accepts the "workspace/repo" spelling of the configured workspace and targets the same repo', async () => {
    const auth = fakeAuth(() => jsonRes({ id: 1, state: 'OPEN', title: 'X' }));
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_get_pr.handler({ repo: 'acme/api', pr_id: 1 });
    expect(r.isError).toBeUndefined();
    expect(auth.bbFetch.mock.calls[0][0].slice(0, 3)).toEqual(['repositories', 'acme', 'api']);
  });

  it('refuses a prefix naming another workspace, and says which workspace is fixed', async () => {
    const auth = fakeAuth(() => jsonRes({}));
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_get_pr.handler({ repo: 'evil/api', pr_id: 1 });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toMatch(/bare repository slug/);
    expect(r.content[0].text).toContain('acme');
    expect(auth.bbFetch).not.toHaveBeenCalled();
  });

  // Normalization strips ONE prefix; what remains must still be an exact allow-list entry, and the
  // message quotes the argument the caller passed rather than the stripped remainder.
  it('does not let a prefixed argument smuggle a path past the allow-list', async () => {
    const auth = fakeAuth(() => jsonRes({}));
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_get_pr.handler({ repo: 'acme/api/../secret', pr_id: 1 });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toContain('"acme/api/../secret"');
    expect(r.content[0].text).toContain('not in the allow-list');
    expect(auth.bbFetch).not.toHaveBeenCalled();
  });

  it('points at the misconfigured allow-list when its entries are workspace-prefixed', async () => {
    const auth = fakeAuth(() => jsonRes({}));
    const s = makeServer();
    register(s, ctx(auth, { moduleConfigs: { bitbucket: { ...CFG, repo_allowlist: 'acme/api' } } }));
    const r = await s.tools.bitbucket_get_pr.handler({ repo: 'api', pr_id: 1 });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toContain('acme/api');
    expect(r.content[0].text).toMatch(/repo_allowlist must hold bare repository slugs/);
    expect(auth.bbFetch).not.toHaveBeenCalled();
  });

  it('ignores a caller-injected workspace and always targets the configured workspace', async () => {
    const auth = fakeAuth(() => jsonRes({ id: 1, state: 'OPEN', title: 'X' }));
    const s = makeServer();
    register(s, ctx(auth));
    // A caller cannot redirect the target: an injected `workspace` key is not in the schema and is ignored;
    // the tool uses the configured workspace ('acme') regardless.
    const r = await s.tools.bitbucket_get_pr.handler({ workspace: 'evil', repo: 'api', pr_id: 1 });
    expect(r.isError).toBeUndefined();
    expect(auth.bbFetch).toHaveBeenCalledTimes(1);
    const segments = auth.bbFetch.mock.calls[0][0];
    expect(segments[0]).toBe('repositories');
    expect(segments[1]).toBe('acme'); // configured workspace, never 'evil'
  });

  it('fail-closed by absence of config: an empty resolved config rejects every repo', async () => {
    const s = makeServer();
    // No bitbucketAuthFactory ⇒ the real createBitbucketAuth runs against an empty config (isConfigured false).
    register(s, { log: vi.fn(), moduleConfigs: { bitbucket: {} }, isToolEnabled: () => true });
    const r = await s.tools.bitbucket_get_pr.handler({ repo: 'api', pr_id: 1 });
    expect(r.isError).toBe(true);
  });

  it('reads a PR for an allow-listed repo', async () => {
    const auth = fakeAuth(() => jsonRes({ id: 1, state: 'OPEN', title: 'X' }));
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_get_pr.handler({ repo: 'api', pr_id: 1 });
    expect(r.isError).toBeUndefined();
    expect(r.content[0].text).toContain('"state": "OPEN"');
  });
});

describe('bitbucket write tools: OPEN-PR gate + request shapes', () => {
  it('add_comment builds an inline comment and only after confirming the PR is OPEN', async () => {
    let posted = null;
    const auth = fakeAuth((segments, opts) => {
      if (opts.method === 'POST' && segments[segments.length - 1] === 'comments') {
        posted = opts.body;
        return jsonRes({ id: 'c-new' }, 201);
      }
      return jsonRes({ state: 'OPEN' }); // requireOpenPr
    });
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_add_comment.handler({
      repo: 'api', pr_id: 42, content: 'fix this', inline: { path: 'a.py', to: 10 },
    });
    expect(r.isError).toBeUndefined();
    expect(posted).toEqual({ content: { raw: 'fix this' }, inline: { path: 'a.py', to: 10 } });
  });

  it('add_comment threads a reply via parent.id', async () => {
    let posted = null;
    const auth = fakeAuth((segments, opts) => {
      if (opts.method === 'POST') { posted = opts.body; return jsonRes({ id: 'c' }, 201); }
      return jsonRes({ state: 'OPEN' });
    });
    const s = makeServer();
    register(s, ctx(auth));
    await s.tools.bitbucket_add_comment.handler({ repo: 'api', pr_id: 42, content: 'ok', parent_id: 5 });
    expect(posted.parent).toEqual({ id: 5 });
  });

  it('write tools reject a non-OPEN PR (no write issued)', async () => {
    const auth = fakeAuth(() => jsonRes({ state: 'MERGED' }));
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_add_comment.handler({ repo: 'api', pr_id: 42, content: 'x' });
    expect(r.isError).toBe(true);
    // only the requireOpenPr GET happened; no POST
    expect(auth.bbFetch).toHaveBeenCalledTimes(1);
  });

  it('set_review hits /approve, /request-changes, or DELETEs both for none', async () => {
    const seen = [];
    const auth = fakeAuth((segments, opts) => {
      const tail = segments[segments.length - 1];
      if (tail === 'approve' || tail === 'request-changes') { seen.push(`${opts.method} ${tail}`); return jsonRes({}); }
      return jsonRes({ state: 'OPEN' });
    });
    const s = makeServer();
    register(s, ctx(auth));
    await s.tools.bitbucket_set_review.handler({ repo: 'api', pr_id: 1, decision: 'approve' });
    await s.tools.bitbucket_set_review.handler({ repo: 'api', pr_id: 1, decision: 'request_changes' });
    await s.tools.bitbucket_set_review.handler({ repo: 'api', pr_id: 1, decision: 'none' });
    expect(seen).toContain('POST approve');
    expect(seen).toContain('POST request-changes');
    expect(seen).toContain('DELETE approve');
    expect(seen).toContain('DELETE request-changes');
  });
});

describe('bitbucket_create_pr: source + destination allow-list (F-sec4)', () => {
  it('creates a same-repo PR', async () => {
    let posted = null;
    const auth = fakeAuth((segments, opts) => {
      if (opts.method === 'POST') { posted = opts.body; return jsonRes({ id: 7, links: { html: { href: 'u' } } }, 201); }
      return jsonRes({});
    });
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_create_pr.handler({
      repo: 'api', title: 'T', source_branch: 'feat', destination_branch: 'main',
    });
    expect(r.isError).toBeUndefined();
    expect(posted.source.branch.name).toBe('feat');
    expect(posted.destination.branch.name).toBe('main');
    expect(posted.source.repository).toBeUndefined();
  });

  it('targets the configured workspace even when one is injected into the args', async () => {
    let segments = null;
    const auth = fakeAuth((segs, opts) => {
      if (opts.method === 'POST') { segments = segs; return jsonRes({ id: 9 }, 201); }
      return jsonRes({});
    });
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_create_pr.handler({
      workspace: 'evil', repo: 'api', title: 'T', source_branch: 'feat',
    });
    expect(r.isError).toBeUndefined();
    expect(segments[1]).toBe('acme'); // configured workspace, never the injected 'evil'
  });

  it('accepts an allow-listed source_repository (cross-repo / fork)', async () => {
    let posted = null;
    const auth = fakeAuth((segments, opts) => {
      if (opts.method === 'POST') { posted = opts.body; return jsonRes({ id: 8 }, 201); }
      return jsonRes({});
    });
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_create_pr.handler({
      repo: 'api', title: 'T', source_branch: 'feat', source_repository: 'acme/web',
    });
    expect(r.isError).toBeUndefined();
    expect(posted.source.repository).toEqual({ full_name: 'acme/web' });
  });

  it('rejects a source_repository outside the allow-list', async () => {
    const auth = fakeAuth(() => jsonRes({}, 201));
    const s = makeServer();
    register(s, ctx(auth));
    const r = await s.tools.bitbucket_create_pr.handler({
      repo: 'api', title: 'T', source_branch: 'feat', source_repository: 'acme/secret',
    });
    expect(r.isError).toBe(true);
    expect(auth.bbFetch).not.toHaveBeenCalled();
  });
});
