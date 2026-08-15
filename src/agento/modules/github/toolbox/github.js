import { z } from 'zod';
import { createGitHubAuth } from './github-auth.js';
import { REVIEW_THREADS_QUERY, collectPages, parseRepoAllowlist } from './api-handlers.js';
// bitbucket.js has no equivalent import — the guard is GitHub-specific and deliberately explicit.
import { offendingEnvKeys } from './env-guard.js';

// The agent's PR write surface — every tool opt-in (isToolEnabled) AND bounded to the SESSION-resolved
// scoped config the toolbox hands every module (ctx.moduleConfigs.github): token + owner +
// repo_allowlist. The owner is NEVER a tool argument — it is fixed by config; the remaining tool args
// are VALIDATED, never TRUSTED — they may only narrow within the allow-list.
const RESOLVE_MUTATION = `
mutation($threadId:ID!){
  resolveReviewThread(input:{threadId:$threadId}){ thread{ id isResolved } }
}`;

export function register(server, { log, moduleConfigs, isToolEnabled, githubAuthFactory }) {
  // The ENV guard runs BEFORE cfg is read: a global CONFIG__GITHUB__* override resolves the same
  // operator-wide credential for every agent_view, so this session gets no GitHub capability at all
  // rather than one bound to the wrong identity. Not a second allow-list — isToolEnabled still gates
  // every tool below; this answer is per-process, not per-tool.
  const offenders = offendingEnvKeys();
  if (offenders.length) {
    log('github', 'WARN', `not registering tools: view-scoped fields set as global ENV overrides: ${offenders.join(', ')}`);
    return;
  }

  const cfg = (moduleConfigs && moduleConfigs.github) || {};
  const auth = (githubAuthFactory || createGitHubAuth)(cfg);
  const owner = cfg.github_owner || null;
  const allowlist = parseRepoAllowlist(cfg.repo_allowlist);

  // At startup (registerModuleRestApis) isToolEnabled is undefined and the server is a stub, so
  // registering is harmless; at session time a disabled tool is skipped entirely (opt-in).
  const enabled = (name) => !isToolEnabled || isToolEnabled(name);

  const idArg = z.union([z.number().int().positive(), z.string().min(1)]);

  // The FORMAT rule belongs in the argument's own description, not only in the tool's prose. "The owner
  // is fixed by configuration" states a policy; a model that has used `gh` still writes "owner/repo"
  // because nothing told it the format. One shared schema keeps the eight tools from drifting apart.
  const repoArgSchema = z.string().min(1).describe(
    'Bare repository name WITHOUT the owner prefix, e.g. "agento" not "openagento/agento". '
    + 'The owner comes from configuration.',
  );

  function err(toolName, msg) {
    log(toolName, 'BLOCKED', msg);
    return { content: [{ type: 'text', text: `Error: ${msg}` }], isError: true };
  }

  function ok(text) {
    return { content: [{ type: 'text', text }] };
  }

  // Fail-closed by config-absence: no token/owner (isConfigured() requires both), or repo not in the
  // resolved allow-list ⇒ rejected. The owner is fixed by config — never a tool argument — so it cannot
  // be caller-influenced. An empty allow-list rejects every repo. Returns `{error}` to return directly,
  // or `{repo}` — the normalized bare name every caller must use from there on.
  //
  // The ARGUMENT is normalized, the ALLOW-LIST never is. "acme/api" under owner "acme" is the agent
  // writing the `gh` spelling of a target it is entitled to: the request URL is built from the
  // configured owner either way, so stripping a prefix that EQUALS that owner cannot redirect the call
  // and cannot widen the allow-list, which stays an exact match on bare names. A prefix naming any
  // OTHER owner is refused by name (it is a real attempt to leave the configured owner), and an
  // owner-prefixed allow-list is still only ever DIAGNOSED — normalizing config entries would be the
  // widening this deliberately avoids.
  function resolveTarget(toolName, repo) {
    if (!auth.isConfigured()) return { error: err(toolName, 'GitHub not configured for this scope') };
    const raw = String(repo);
    let name = raw;
    const slash = raw.indexOf('/');
    if (slash !== -1) {
      const prefix = raw.slice(0, slash);
      if (prefix !== String(owner)) {
        return {
          error: err(
            toolName,
            `repo "${raw}" must be a bare repository name; the owner is fixed to "${owner}" by the github_owner config`,
          ),
        };
      }
      name = raw.slice(slash + 1);
    }
    if (!allowlist.includes(name)) {
      const malformed = allowlist.filter((r) => r.includes('/'));
      const hint = malformed.length
        ? ` — the configured allow-list holds owner-prefixed entries (${malformed.join(', ')}); repo_allowlist must hold bare repository names`
        : '';
      // Quote what the CALLER passed: it is what they have to correct, and the stripped remainder of a
      // malformed argument ("api/x") reads as a name they never wrote.
      return { error: err(toolName, `repo "${raw}" is not in the allow-list${hint}`) };
    }
    return { repo: name };
  }

  async function getJson(toolName, segments, opts) {
    const r = await auth.ghFetch(segments, opts);
    if (!r.ok) throw await auth.describeError(r);
    return r.json();
  }

  // Write-tool gate: re-fetch the PR and reject anything but an open PR. Returns `{error}` to return
  // directly, or `{pr}` — the caller needs `head.sha` for a new inline comment.
  async function requireOpenPr(toolName, repo, prNumber) {
    let pr;
    try {
      pr = await getJson(toolName, ['repos', owner, repo, 'pulls', prNumber]);
    } catch (e) {
      return { error: err(toolName, `could not load PR ${prNumber}: ${e.message}`) };
    }
    if (pr.state !== 'open') return { error: err(toolName, `PR ${prNumber} is not open (state=${pr.state})`) };
    return { pr };
  }

  // `server.tool()` takes a raw SHAPE (sdk server/mcp.d.ts:136), so the refined schema cannot be what is
  // registered — a ZodEffects would resolve as the annotations overload and publish a tool with no
  // parameters. The shape is registered; the cross-field rule is enforced in the handler.
  const addCommentShape = {
    repo: repoArgSchema,
    pr_number: idArg,
    content: z.string().min(1),
    in_reply_to: idArg.optional(),
    inline: z
      .object({
        path: z.string().min(1),
        line: z.number().int().positive(),
        side: z.enum(['LEFT', 'RIGHT']).optional(),
      })
      .optional(),
  };

  // The constraint is BETWEEN two fields, so the whole object is refined, not one field.
  const addCommentSchema = z.object(addCommentShape).refine((a) => !(a.inline && a.in_reply_to !== undefined), {
    message: 'pass either `inline` (a new line-anchored comment) or `in_reply_to` (a reply), not both',
    path: ['inline'],
  });

  // --- reads ---------------------------------------------------------------------------------------
  if (enabled('github_get_pr')) {
    server.tool(
      'github_get_pr',
      'Read a pull request (title, body, state, head/base branches).',
      { repo: repoArgSchema, pr_number: idArg },
      async ({ repo: repoArg, pr_number: prNumber }) => {
        const target = resolveTarget('github_get_pr', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        try {
          const pr = await getJson('github_get_pr', ['repos', owner, repo, 'pulls', prNumber]);
          log('github_get_pr', 'OK', `${owner}/${repo}#${prNumber}`);
          return ok(JSON.stringify(pr, null, 2));
        } catch (e) {
          return err('github_get_pr', `read failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('github_get_pr_diff')) {
    server.tool(
      'github_get_pr_diff',
      "Read a pull request's diff (unified diff text).",
      { repo: repoArgSchema, pr_number: idArg },
      async ({ repo: repoArg, pr_number: prNumber }) => {
        const target = resolveTarget('github_get_pr_diff', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        try {
          const r = await auth.ghFetch(['repos', owner, repo, 'pulls', prNumber], {
            accept: 'application/vnd.github.diff',
          });
          if (!r.ok) {
            // 406 is GitHub's answer for a diff it refuses to generate (too large) — a distinct,
            // actionable outcome, not a generic read failure.
            if (r.status === 406) {
              // Drain before returning: this is the one branch that does not go through
              // describeError, and an unread body can hold the socket open until GC.
              await r.text().catch(() => '');
              return err('github_get_pr_diff', 'diff is too large for the API to generate; review the files individually');
            }
            throw await auth.describeError(r);
          }
          const diff = await r.text();
          log('github_get_pr_diff', 'OK', `${owner}/${repo}#${prNumber}`);
          return ok(diff);
        } catch (e) {
          return err('github_get_pr_diff', `read failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('github_get_pr_comments')) {
    server.tool(
      'github_get_pr_comments',
      "Read a pull request's comments — the conversation thread AND the inline review comments.",
      { repo: repoArgSchema, pr_number: idArg },
      async ({ repo: repoArg, pr_number: prNumber }) => {
        const target = resolveTarget('github_get_pr_comments', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        try {
          // GitHub splits PR feedback across two REST surfaces; returning one of them would silently
          // hide half the feedback the publisher already acted on.
          const surfaces = [
            ['conversation', ['repos', owner, repo, 'issues', prNumber, 'comments']],
            ['inline', ['repos', owner, repo, 'pulls', prNumber, 'comments']],
          ];
          const comments = [];
          const truncated = [];
          for (const [surface, segments] of surfaces) {
            const page = await collectPages(auth, segments, {}, { maxPages: 5 });
            for (const c of page.items) comments.push({ ...c, surface });
            if (page.truncated) truncated.push(surface);
          }
          log('github_get_pr_comments', 'OK', `${owner}/${repo}#${prNumber} n=${comments.length}`);
          return ok(JSON.stringify({ comments, truncated }, null, 2));
        } catch (e) {
          return err('github_get_pr_comments', `read failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('github_get_pr_reviews')) {
    server.tool(
      'github_get_pr_reviews',
      "Read a pull request's reviews / review history.",
      { repo: repoArgSchema, pr_number: idArg },
      async ({ repo: repoArg, pr_number: prNumber }) => {
        const target = resolveTarget('github_get_pr_reviews', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        try {
          // The review list is append-only and unbounded on an active PR, so an unpaginated read is
          // exactly the case where the newest (decisive) entries fall off the first page.
          const { items, truncated } = await collectPages(
            auth, ['repos', owner, repo, 'pulls', prNumber, 'reviews'], {}, { maxPages: 5 },
          );
          log('github_get_pr_reviews', 'OK', `${owner}/${repo}#${prNumber} n=${items.length}`);
          return ok(JSON.stringify({ reviews: items, truncated }, null, 2));
        } catch (e) {
          return err('github_get_pr_reviews', `read failed: ${e.message}`);
        }
      },
    );
  }

  // --- writes (each re-checks the PR is open) -------------------------------------------------------
  if (enabled('github_add_comment')) {
    server.tool(
      'github_add_comment',
      [
        'Reply on a pull request. Omit inline + in_reply_to for a conversation comment; pass in_reply_to',
        'to reply inside an existing review thread; pass inline { path, line, side } for a new',
        'line-anchored review comment. inline and in_reply_to are mutually exclusive.',
      ].join('\n'),
      addCommentShape,
      async (args) => {
        // First statement, so a schema-rejected call never reaches the open-state GET, let alone a write.
        const parsed = addCommentSchema.safeParse(args);
        if (!parsed.success) return err('github_add_comment', parsed.error.issues[0].message);
        const { repo: repoArg, pr_number: prNumber, content, in_reply_to: inReplyTo, inline } = parsed.data;
        const target = resolveTarget('github_add_comment', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        const gate = await requireOpenPr('github_add_comment', repo, prNumber);
        if (gate.error) return gate.error;
        try {
          let segments;
          let body;
          if (inline) {
            // A new line-anchored comment must name the commit it annotates; the open-state gate
            // already fetched the PR, so the head SHA is in hand.
            segments = ['repos', owner, repo, 'pulls', prNumber, 'comments'];
            body = { body: content, path: inline.path, line: inline.line, commit_id: gate.pr.head?.sha };
            if (inline.side) body.side = inline.side;
          } else if (inReplyTo !== undefined) {
            segments = ['repos', owner, repo, 'pulls', prNumber, 'comments'];
            body = { body: content, in_reply_to: inReplyTo };
          } else {
            segments = ['repos', owner, repo, 'issues', prNumber, 'comments'];
            body = { body: content };
          }
          const r = await auth.ghFetch(segments, { method: 'POST', body });
          if (!r.ok) throw await auth.describeError(r);
          log('github_add_comment', 'OK', `${owner}/${repo}#${prNumber}`);
          return ok('Comment posted.');
        } catch (e) {
          return err('github_add_comment', `post failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('github_resolve_thread')) {
    server.tool(
      'github_resolve_thread',
      'Resolve the review thread containing a given review comment (GraphQL — REST cannot do this).',
      { repo: repoArgSchema, pr_number: idArg, comment_id: idArg },
      async ({ repo: repoArg, pr_number: prNumber, comment_id: commentId }) => {
        const target = resolveTarget('github_resolve_thread', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        const gate = await requireOpenPr('github_resolve_thread', repo, prNumber);
        if (gate.error) return gate.error;
        let data;
        try {
          data = await auth.ghGraphql(REVIEW_THREADS_QUERY, {
            owner, name: repo, number: Number(prNumber),
          });
        } catch (e) {
          return err('github_resolve_thread', `thread lookup failed: ${e.message}`);
        }
        const threadsPage = data?.repository?.pullRequest?.reviewThreads;
        const nodes = threadsPage?.nodes || [];
        const bounded = !!threadsPage?.pageInfo?.hasNextPage
          || nodes.some((t) => t?.comments?.pageInfo?.hasNextPage);
        // The argument may arrive as a number or a string, so compare as strings.
        const wanted = String(commentId);
        // `fullDatabaseId` is the non-deprecated id (a BigInt delivered as a STRING), and comparing
        // strings keeps this path exact for ids past 2^53 — unlike the REST-matching path, nothing here
        // ever converts it to a double.
        const thread = nodes.find((t) => (t?.comments?.nodes || []).some(
          (c) => String(c?.fullDatabaseId) === wanted,
        ));
        if (!thread) {
          // A bounded result never reports a definitive miss: "does not exist" and "I did not look far
          // enough" call for opposite next moves from the agent.
          return err(
            'github_resolve_thread',
            bounded
              ? `could not determine the thread for comment ${wanted}: this PR has more review threads or comments than one query returns (100). Nothing was resolved.`
              : `no review thread contains comment ${wanted}`,
          );
        }
        try {
          await auth.ghGraphql(RESOLVE_MUTATION, { threadId: thread.id });
          log('github_resolve_thread', 'OK', `${owner}/${repo}#${prNumber} c=${wanted}`);
          return ok('Review thread resolved.');
        } catch (e) {
          return err('github_resolve_thread', `resolve failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('github_set_review')) {
    server.tool(
      'github_set_review',
      'Submit a review decision on a pull request: approve, request_changes, or comment.',
      {
        repo: repoArgSchema,
        pr_number: idArg,
        decision: z.enum(['approve', 'request_changes', 'comment']),
        body: z.string().optional(),
      },
      async ({ repo: repoArg, pr_number: prNumber, decision, body }) => {
        const target = resolveTarget('github_set_review', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        // GitHub requires a body for REQUEST_CHANGES and COMMENT; catching it here turns a 422 into a
        // message the agent can act on.
        if (decision !== 'approve' && (!body || !String(body).trim())) {
          return err('github_set_review', `decision "${decision}" requires a non-empty body`);
        }
        const gate = await requireOpenPr('github_set_review', repo, prNumber);
        if (gate.error) return gate.error;
        const event = { approve: 'APPROVE', request_changes: 'REQUEST_CHANGES', comment: 'COMMENT' }[decision];
        try {
          const payload = { event };
          if (body) payload.body = body;
          const r = await auth.ghFetch(['repos', owner, repo, 'pulls', prNumber, 'reviews'], {
            method: 'POST', body: payload,
          });
          if (!r.ok) {
            const failure = await auth.describeError(r);
            // 422 is GitHub's blanket validation status — it also covers a stale commit_id, an
            // already-pending review, and an event invalid for the PR's state. GitHub's own message
            // (carried by `failure`) is what tells those apart, so it leads; the hint follows as the
            // likeliest cause, not the only one.
            if (r.status === 422) {
              return err(
                'github_set_review',
                `GitHub rejected the review (422 validation failed): ${failure.message} — one common cause is that you cannot approve or request changes on your own pull request`,
              );
            }
            throw failure;
          }
          log('github_set_review', 'OK', `${owner}/${repo}#${prNumber} ${decision}`);
          return ok(`Review submitted: ${decision}.`);
        } catch (e) {
          return err('github_set_review', `set_review failed: ${e.message}`);
        }
      },
    );
  }

  if (enabled('github_create_pr')) {
    server.tool(
      'github_create_pr',
      [
        'Open a new pull request. The owner is fixed by configuration and the repo must be in the',
        'allow-list. head_owner may only be the configured owner (cross-owner forks are refused).',
      ].join('\n'),
      {
        repo: repoArgSchema,
        title: z.string().min(1),
        head_branch: z.string().min(1),
        base_branch: z.string().min(1),
        body: z.string().optional(),
        head_owner: z.string().optional(),
      },
      async ({ repo: repoArg, title, head_branch: head, base_branch: base, body, head_owner: headOwner }) => {
        const target = resolveTarget('github_create_pr', repoArg);
        if (target.error) return target.error;
        const repo = target.repo;
        // The allow-list only bounds repos INSIDE the configured owner, so it cannot authorize a fork
        // living under a different owner.
        if (headOwner && String(headOwner) !== String(owner)) {
          return err('github_create_pr', `head_owner "${headOwner}" is not the configured owner`);
        }
        // GitHub's cross-repo form for `head` is "owner:branch". Validating only `head_owner` would
        // leave that owner half settable through the branch argument instead — a git ref cannot contain
        // ':' anyway, so refusing it costs nothing and closes the second spelling of the same argument.
        if (String(head).includes(':')) {
          return err(
            'github_create_pr',
            'head_branch must be a bare branch name (no ":"); use head_owner for the cross-repo form',
          );
        }
        try {
          const payload = { title, head: headOwner ? `${headOwner}:${head}` : head, base };
          if (body) payload.body = body;
          const r = await auth.ghFetch(['repos', owner, repo, 'pulls'], { method: 'POST', body: payload });
          if (!r.ok) throw await auth.describeError(r);
          const created = await r.json();
          log('github_create_pr', 'OK', `${owner}/${repo} #${created.number}`);
          return ok(`PR created: #${created.number} ${created.html_url || ''}`.trim());
        } catch (e) {
          return err('github_create_pr', `create failed: ${e.message}`);
        }
      },
    );
  }
}
