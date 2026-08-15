import { z } from 'zod';
import { createBitbucketAuth } from './bitbucket-auth.js';
import { parseRepoAllowlist } from './api-handlers.js';

// The agent's PR write surface — every tool opt-in (isToolEnabled) AND bounded to the SESSION-resolved
// scoped config the toolbox hands every module (ctx.moduleConfigs.bitbucket): creds + workspace +
// repo_allowlist. The workspace is NEVER a tool argument — it is fixed by config; the remaining tool
// args are VALIDATED, never TRUSTED — they may only narrow within the allow-list.
export function register(server, { log, moduleConfigs, isToolEnabled, bitbucketAuthFactory }) {
  const cfg = (moduleConfigs && moduleConfigs.bitbucket) || {};
  const auth = (bitbucketAuthFactory || createBitbucketAuth)(cfg);
  const workspace = cfg.bitbucket_workspace || null;
  const allowlist = parseRepoAllowlist(cfg.repo_allowlist);

  // At startup (registerModuleRestApis) isToolEnabled is undefined and the server is a stub, so
  // registering is harmless; at session time a disabled tool is skipped entirely (opt-in).
  const enabled = (name) => !isToolEnabled || isToolEnabled(name);

  const idArg = z.union([z.number(), z.string()]);

  // The FORMAT rule belongs in the argument's own description, not only in the tool's prose — a model
  // that knows the "workspace/repo" spelling from the Bitbucket UI writes it unless told otherwise.
  // One shared schema keeps the eight tools from drifting apart.
  const repoArgSchema = z.string().min(1).describe(
    'Bare repository slug WITHOUT the workspace prefix, e.g. "api" not "acme/api". '
    + 'The workspace comes from configuration.',
  );

  function err(toolName, msg) {
    log(toolName, 'BLOCKED', msg);
    return { content: [{ type: 'text', text: `Error: ${msg}` }], isError: true };
  }

  // Fail-closed by config-absence: no creds (isConfigured() already requires a configured workspace),
  // or repo not in the resolved allow-list ⇒ rejected. The workspace is fixed by config — never a tool
  // argument — so it cannot be caller-influenced. An empty allow-list rejects every repo. Returns
  // `{error}` to return directly, or `{repo}` — the normalized bare slug every caller uses from there on.
  //
  // The ARGUMENT is normalized, the ALLOW-LIST never is. "acme/api" under workspace "acme" is the agent
  // writing the UI spelling of a target it is entitled to: the request URL is built from the configured
  // workspace either way, so stripping a prefix that EQUALS that workspace cannot redirect the call and
  // cannot widen the allow-list, which stays an exact match on bare slugs. A prefix naming any OTHER
  // workspace is refused by name, and a workspace-prefixed allow-list is still only ever DIAGNOSED —
  // normalizing config entries would be the widening this deliberately avoids.
  function resolveTarget(toolName, repo) {
    if (!auth.isConfigured()) return { error: err(toolName, 'Bitbucket not configured for this scope') };
    const raw = String(repo);
    let slug = raw;
    const slash = raw.indexOf('/');
    if (slash !== -1) {
      const prefix = raw.slice(0, slash);
      if (prefix !== String(workspace)) {
        return {
          error: err(
            toolName,
            `repo "${raw}" must be a bare repository slug; the workspace is fixed to "${workspace}" by the bitbucket_workspace config`,
          ),
        };
      }
      slug = raw.slice(slash + 1);
    }
    if (!allowlist.includes(slug)) {
      const malformed = allowlist.filter((r) => r.includes('/'));
      const hint = malformed.length
        ? ` — the configured allow-list holds workspace-prefixed entries (${malformed.join(', ')}); repo_allowlist must hold bare repository slugs`
        : '';
      // Quote what the CALLER passed — the stripped remainder of a malformed argument reads as a slug
      // they never wrote.
      return { error: err(toolName, `repo "${raw}" is not in the allow-list${hint}`) };
    }
    return { repo: slug };
  }

  async function getJson(toolName, segments) {
    const r = await auth.bbFetch(segments);
    if (!r.ok) {
      await r.text().catch(() => '');
      throw new Error(`HTTP ${r.status}`);
    }
    return r.json();
  }

  // Write-tool gate: re-fetch the PR and reject anything but an OPEN PR (F-sec3). Returns an MCP error
  // object to return directly, or null when the PR is OPEN.
  async function requireOpenPr(toolName, repo, prId) {
    let pr;
    try {
      pr = await getJson(toolName, ['repositories', workspace, repo, 'pullrequests', prId]);
    } catch (e) {
      return err(toolName, `could not load PR ${prId}: ${e.message}`);
    }
    if (pr.state !== 'OPEN') return err(toolName, `PR ${prId} is not OPEN (state=${pr.state})`);
    return null;
  }

  function ok(text) {
    return { content: [{ type: 'text', text }] };
  }

  // --- reads ---------------------------------------------------------------------------------------
  if (enabled('bitbucket_get_pr')) {
    server.tool(
      'bitbucket_get_pr',
      'Read a pull request (title, description, state, source/destination branches).',
      { repo: repoArgSchema, pr_id: idArg },
      async ({ repo: repoArg, pr_id: prId }) => {
        const target = resolveTarget('bitbucket_get_pr', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        try {
          const pr = await getJson('bitbucket_get_pr', ['repositories', workspace, repo, 'pullrequests', prId]);
          log('bitbucket_get_pr', 'OK', `${workspace}/${repo}#${prId}`);
          return ok(JSON.stringify(pr, null, 2));
        } catch (e) {
          return err('bitbucket_get_pr', `read failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('bitbucket_get_pr_diff')) {
    server.tool(
      'bitbucket_get_pr_diff',
      "Read a pull request's diff (unified diff text).",
      { repo: repoArgSchema, pr_id: idArg },
      async ({ repo: repoArg, pr_id: prId }) => {
        const target = resolveTarget('bitbucket_get_pr_diff', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        try {
          const r = await auth.bbFetch(['repositories', workspace, repo, 'pullrequests', prId, 'diff']);
          if (!r.ok) {
            await r.text().catch(() => '');
            throw new Error(`HTTP ${r.status}`);
          }
          const diff = await r.text();
          log('bitbucket_get_pr_diff', 'OK', `${workspace}/${repo}#${prId}`);
          return ok(diff);
        } catch (e) {
          return err('bitbucket_get_pr_diff', `read failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('bitbucket_get_pr_comments')) {
    server.tool(
      'bitbucket_get_pr_comments',
      "Read a pull request's comments.",
      { repo: repoArgSchema, pr_id: idArg },
      async ({ repo: repoArg, pr_id: prId }) => {
        const target = resolveTarget('bitbucket_get_pr_comments', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        try {
          const data = await getJson('bitbucket_get_pr_comments', [
            'repositories', workspace, repo, 'pullrequests', prId, 'comments',
          ]);
          log('bitbucket_get_pr_comments', 'OK', `${workspace}/${repo}#${prId}`);
          return ok(JSON.stringify(data.values || data, null, 2));
        } catch (e) {
          return err('bitbucket_get_pr_comments', `read failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('bitbucket_get_pr_activity')) {
    server.tool(
      'bitbucket_get_pr_activity',
      "Read a pull request's activity / review history.",
      { repo: repoArgSchema, pr_id: idArg },
      async ({ repo: repoArg, pr_id: prId }) => {
        const target = resolveTarget('bitbucket_get_pr_activity', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        try {
          const data = await getJson('bitbucket_get_pr_activity', [
            'repositories', workspace, repo, 'pullrequests', prId, 'activity',
          ]);
          log('bitbucket_get_pr_activity', 'OK', `${workspace}/${repo}#${prId}`);
          return ok(JSON.stringify(data.values || data, null, 2));
        } catch (e) {
          return err('bitbucket_get_pr_activity', `read failed: ${e.message}`);
        }
      },
    );
  }

  // --- writes (each re-checks the PR is OPEN) ------------------------------------------------------
  if (enabled('bitbucket_add_comment')) {
    server.tool(
      'bitbucket_add_comment',
      [
        'Reply on a pull request. Omit parent_id + inline for a top-level comment; pass parent_id to',
        'reply in a thread; pass inline { path, to } for an inline file:line comment.',
      ].join('\n'),
      {
        repo: repoArgSchema,
        pr_id: idArg,
        content: z.string(),
        parent_id: idArg.optional(),
        inline: z.object({ path: z.string(), to: z.number().optional() }).optional(),
      },
      async ({ repo: repoArg, pr_id: prId, content, parent_id: parentId, inline }) => {
        const target = resolveTarget('bitbucket_add_comment', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        const open = await requireOpenPr('bitbucket_add_comment', repo, prId);
        if (open) return open;
        try {
          const body = { content: { raw: content } };
          if (parentId !== undefined) body.parent = { id: parentId };
          if (inline) body.inline = inline;
          const r = await auth.bbFetch(
            ['repositories', workspace, repo, 'pullrequests', prId, 'comments'],
            { method: 'POST', body },
          );
          if (!r.ok) {
            await r.text().catch(() => '');
            throw new Error(`HTTP ${r.status}`);
          }
          log('bitbucket_add_comment', 'OK', `${workspace}/${repo}#${prId}`);
          return ok('Comment posted.');
        } catch (e) {
          return err('bitbucket_add_comment', `post failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('bitbucket_resolve_comment')) {
    server.tool(
      'bitbucket_resolve_comment',
      'Resolve a pull request comment thread.',
      { repo: repoArgSchema, pr_id: idArg, comment_id: idArg },
      async ({ repo: repoArg, pr_id: prId, comment_id: commentId }) => {
        const target = resolveTarget('bitbucket_resolve_comment', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        const open = await requireOpenPr('bitbucket_resolve_comment', repo, prId);
        if (open) return open;
        try {
          const r = await auth.bbFetch(
            ['repositories', workspace, repo, 'pullrequests', prId, 'comments', commentId, 'resolve'],
            { method: 'POST' },
          );
          if (!r.ok) {
            await r.text().catch(() => '');
            throw new Error(`HTTP ${r.status}`);
          }
          log('bitbucket_resolve_comment', 'OK', `${workspace}/${repo}#${prId} c=${commentId}`);
          return ok('Comment resolved.');
        } catch (e) {
          return err('bitbucket_resolve_comment', `resolve failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('bitbucket_set_review')) {
    server.tool(
      'bitbucket_set_review',
      'Set the review decision on a pull request: approve, request_changes, or none (retract).',
      {
        repo: repoArgSchema,
        pr_id: idArg,
        decision: z.enum(['approve', 'request_changes', 'none']),
      },
      async ({ repo: repoArg, pr_id: prId, decision }) => {
        const target = resolveTarget('bitbucket_set_review', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        const open = await requireOpenPr('bitbucket_set_review', repo, prId);
        if (open) return open;
        const prSeg = ['repositories', workspace, repo, 'pullrequests', prId];
        try {
          if (decision === 'approve') {
            const r = await auth.bbFetch([...prSeg, 'approve'], { method: 'POST' });
            if (!r.ok) {
              await r.text().catch(() => '');
              throw new Error(`HTTP ${r.status}`);
            }
          } else if (decision === 'request_changes') {
            const r = await auth.bbFetch([...prSeg, 'request-changes'], { method: 'POST' });
            if (!r.ok) {
              await r.text().catch(() => '');
              throw new Error(`HTTP ${r.status}`);
            }
          } else {
            // none: retract both decisions; Bitbucket has no single state-clear endpoint, and a 404
            // ("not present") on either is fine.
            for (const seg of ['approve', 'request-changes']) {
              const r = await auth.bbFetch([...prSeg, seg], { method: 'DELETE' });
              if (!r.ok && r.status !== 404) {
                await r.text().catch(() => '');
                throw new Error(`HTTP ${r.status}`);
              }
            }
          }
          log('bitbucket_set_review', 'OK', `${workspace}/${repo}#${prId} ${decision}`);
          return ok(`Review decision set: ${decision}.`);
        } catch (e) {
          return err('bitbucket_set_review', `set_review failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('bitbucket_create_pr')) {
    server.tool(
      'bitbucket_create_pr',
      [
        'Open a new pull request. The destination workspace is fixed by configuration; the destination',
        'repo must be in the allow-list. For a cross-repo / fork PR, source_repository ("workspace/repo")',
        'must ALSO be in the allow-list and its workspace half must equal the configured workspace.',
      ].join('\n'),
      {
        repo: repoArgSchema,
        title: z.string(),
        source_branch: z.string(),
        destination_branch: z.string().optional(),
        description: z.string().optional(),
        source_repository: z.string().optional(),
      },
      async ({
        repo: repoArg, title, source_branch: src, destination_branch: dest,
        description, source_repository: sourceRepo,
      }) => {
        const target = resolveTarget('bitbucket_create_pr', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        // Validate the source repository too when given (forks / cross-repo). Format: "workspace/repo".
        let sourceRepository;
        if (sourceRepo) {
          const parts = String(sourceRepo).split('/');
          if (parts.length !== 2 || parts[0] !== String(workspace) || !allowlist.includes(parts[1])) {
            return err('bitbucket_create_pr', `source_repository "${sourceRepo}" is not in the allow-list`);
          }
          sourceRepository = { full_name: `${parts[0]}/${parts[1]}` };
        }
        try {
          const body = { title, source: { branch: { name: src } } };
          if (sourceRepository) body.source.repository = sourceRepository;
          if (dest) body.destination = { branch: { name: dest } };
          if (description) body.summary = { raw: description };
          const r = await auth.bbFetch(
            ['repositories', workspace, repo, 'pullrequests'],
            { method: 'POST', body },
          );
          if (!r.ok) {
            await r.text().catch(() => '');
            throw new Error(`HTTP ${r.status}`);
          }
          const created = await r.json();
          log('bitbucket_create_pr', 'OK', `${workspace}/${repo} #${created.id}`);
          return ok(`PR created: #${created.id} ${created.links?.html?.href || ''}`.trim());
        } catch (e) {
          return err('bitbucket_create_pr', `create failed: ${e.message}`);
        }
      },
    );
  }
}
