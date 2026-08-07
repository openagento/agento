import { z } from 'zod';
import { readFile } from 'node:fs/promises';
import { basename, join } from 'node:path';

// Resolve the attachment download limit to a non-negative integer.
// Invalid input (negative, partial-numeric like "3abc", empty, null/undefined) falls back to 10.
// `0` means "no limit" and is preserved. (Config values arrive as numbers from config.json
// or strings from DB/ENV; parseInt would silently accept "-5"/"3abc", so use Number + guards.)
function parseDownloadLimit(raw) {
  if (raw === null || raw === undefined) return 10;
  if (typeof raw === 'string' && raw.trim() === '') return 10;
  const num = Number(raw);
  return Number.isInteger(num) && num >= 0 ? num : 10;
}

const ATTACHMENT_DOWNLOAD_CONCURRENCY = 6;

// Jira issue keys = a project key (letter then letters/digits) + "-" + number.
// Validated before issue_key becomes a filesystem path below (anti path-traversal / least-privilege).
const JIRA_KEY_RE = /^[A-Za-z][A-Za-z0-9]*-\d+$/;

// Run `worker` over `items` with at most `limit` concurrent in-flight calls.
// Used to bound parallel attachment downloads (avoids hammering Jira / the FS).
async function mapWithConcurrency(items, limit, worker) {
  const results = new Array(items.length);
  let next = 0;
  const run = async () => {
    while (true) {
      const i = next++;
      if (i >= items.length) return;
      results[i] = await worker(items[i], i);
    }
  };
  const size = Math.max(1, Math.min(limit, items.length));
  await Promise.all(Array.from({ length: size }, run));
  return results;
}

export async function healthcheck({ moduleConfigs }) {
  const cfg = moduleConfigs?.jira || {};
  const config = {
    host: cfg.jira_host || null,
    user: cfg.jira_user || null,
    token: cfg.jira_token || null,
  };

  if (!config.host || !config.token) {
    return [{ tool: 'jira', status: 'skip', error: 'not configured' }];
  }

  const start = Date.now();
  try {
    const auth = Buffer.from(`${config.user}:${config.token}`).toString('base64');
    const response = await fetch(`${config.host}/rest/api/2/myself`, {
      headers: { 'Authorization': `Basic ${auth}`, 'Accept': 'application/json' },
    });
    if (!response.ok) {
      return [{ tool: 'jira', status: 'fail', ms: Date.now() - start, error: `HTTP ${response.status}` }];
    }
    return [{ tool: 'jira', status: 'ok', ms: Date.now() - start }];
  } catch (err) {
    return [{ tool: 'jira', status: 'fail', ms: Date.now() - start, error: err.message }];
  }
}

export function register(server, { log, moduleConfigs, isToolEnabled, artifactsDir, fileManager }) {
  const cfg = moduleConfigs?.jira || {};
  const config = {
    host: cfg.jira_host || null,
    user: cfg.jira_user || null,
    token: cfg.jira_token || null,
  };
  const enabled = (name) => !isToolEnabled || isToolEnabled(name);
  const attachmentDownloadLimit = parseDownloadLimit(cfg.attachment_download_limit);
  // --- jira_search ---
  if (enabled('jira_search')) {
    server.tool(
      'jira_search',
      [
        'Search Jira issues by text in summary. Returns key, summary, status, updated date, and description.',
        'Examples:',
        '  search_term: "synchronizacja stanów"',
        '  search_term: "błąd cen produktu"',
        '  search_term: "94916"',
      ].join('\n'),
      {
        user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
        search_term: z.string().describe('Text to search in issue summaries'),
      },
      async ({ user, search_term }) => {
        const auth = Buffer.from(`${config.user}:${config.token}`).toString('base64');

        try {
          const response = await fetch(`${config.host}/rest/api/3/search/jql`, {
            method: 'POST',
            headers: {
              'Authorization': `Basic ${auth}`,
              'Accept': 'application/json',
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({
              jql: `summary ~ "${search_term}"`,
              fields: ['key', 'summary', 'status', 'assignee', 'updated', 'description'],
            }),
          });

          const data = await response.json();
          const issues = (data.issues || []).map(issue => ({
            Key: issue.key,
            Summary: issue.fields.summary,
            Status: issue.fields.status?.name,
            Updated: issue.fields.updated,
            Description: issue.fields.description,
          }));

          log('jira_search', 'OK', `user=${user} "${search_term}" -> ${issues.length} results`);
          return { content: [{ type: 'text', text: JSON.stringify(issues, null, 2) }] };
        } catch (err) {
          log('jira_search', 'ERROR', `user=${user} ${err.message}`);
          return {
            content: [{ type: 'text', text: `Jira search error: ${err.message}` }],
            isError: true,
          };
        }
      }
    );
  }

  // --- jira_get_issue ---
  if (enabled('jira_get_issue')) {
    server.tool(
      'jira_get_issue',
      [
        'Get full details of a Jira issue by key. Returns summary, status, assignee (with accountId), reporter (with accountId), priority, dates, description, comments, and attachments.',
        'Attachments are automatically downloaded to the artifacts directory. Binary files (PDF, XLSX) are auto-converted to text formats (MD, CSV) when converters are available.',
        'Image attachments are referenced in description/comments as [Obrazek: filename](local_path). To view an image, use the Read tool on the local path.',
        'For converted files, use the Read tool on convertedPath (e.g. .md for PDF, .csv for XLSX).',
        'Comments include author accountId — use it with jira_add_comment reply_to_comment_id to reply.',
        'If an attachment fails to download or convert, Attachments[].error contains the reason. In that case, inform the reporter via comment — do not silently skip the data.',
        'Only the newest N attachments are auto-downloaded (N = jira/attachment_download_limit, default 10; 0 = all). Older ones are listed with an error telling you to call jira_get_attachment to fetch them on demand.',
        'AssigneeAccountId and ReporterAccountId can be used with jira_assign_issue.',
        'Examples:',
        '  issue_key: "AI-1"',
        '  issue_key: "DEV-542"',
      ].join('\n'),
      {
        user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
        issue_key: z.string().describe('Jira issue key, e.g. "AI-1"'),
      },
      async ({ user, issue_key }) => {
        const auth = Buffer.from(`${config.user}:${config.token}`).toString('base64');
        const authHeader = { 'Authorization': `Basic ${auth}`, 'Accept': 'application/json' };

        try {
          const response = await fetch(
            `${config.host}/rest/api/2/issue/${encodeURIComponent(issue_key)}?fields=summary,status,assignee,reporter,priority,created,updated,description,comment,attachment`,
            { headers: authHeader }
          );

          if (!response.ok) {
            const text = await response.text();
            throw new Error(`HTTP ${response.status}: ${text}`);
          }

          const data = await response.json();
          const f = data.fields;

          // Full attachment list — metadata for ALL attachments, regardless of the download limit.
          const allAttachments = f.attachment || [];

          // Select which to physically download: the N newest by `created` (descending).
          // attachmentDownloadLimit === 0 means "no limit" (download all). Anything not
          // selected is reported below with an explicit error — never a silent null.
          const sortedByNewest = [...allAttachments].sort(
            (a, b) => new Date(b.created || 0) - new Date(a.created || 0)
          );
          const toDownload = attachmentDownloadLimit === 0
            ? sortedByNewest
            : sortedByNewest.slice(0, attachmentDownloadLimit);
          const downloadIds = new Set(toDownload.map((att) => att.id));

          const fileMap = new Map();   // att.id -> FileManager success result
          const errorMap = new Map();  // att.id -> real download/convert failure reason
          const imageMap = new Map();  // filename -> localPath (for inline image refs)

          if (toDownload.length > 0 && fileManager) {
            const dir = `${artifactsDir}/jira/${data.key}`;

            await mapWithConcurrency(toDownload, ATTACHMENT_DOWNLOAD_CONCURRENCY, async (att) => {
              try {
                const result = await fileManager.download(att.content, att.filename, {
                  headers: authHeader,
                  dir,
                  maxSize: att.size,
                });
                if (result.skipped) {
                  errorMap.set(att.id, result.skipReason);
                  log('jira_get_issue', 'WARN', `Attachment skipped: ${att.filename}: ${result.skipReason}`);
                } else {
                  fileMap.set(att.id, result);
                  imageMap.set(att.filename, result.localPath);
                  if (result.conversionError) {
                    log('jira_get_issue', 'WARN', `Attachment conversion failed: ${att.filename}: ${result.conversionError}`);
                  }
                }
              } catch (err) {
                errorMap.set(att.id, err.message);
                log('jira_get_issue', 'ERROR', `Attachment download threw: ${att.filename}: ${err.message}`);
              }
            });
          }

          const replaceImageRefs = (text) => {
            if (!text || typeof text !== 'string') return text;
            return text.replace(/!([^|!\n]+)(\|[^!\n]*)?!/g, (match, filename) => {
              const localPath = imageMap.get(filename);
              return localPath ? `[Obrazek: ${filename}](${localPath})` : match;
            });
          };

          const issue = {
            Key: data.key,
            Summary: f.summary,
            Status: f.status?.name,
            Assignee: f.assignee?.displayName || null,
            AssigneeAccountId: f.assignee?.accountId || null,
            Reporter: f.reporter?.displayName || null,
            ReporterAccountId: f.reporter?.accountId || null,
            Priority: f.priority?.name || null,
            Created: f.created,
            Updated: f.updated,
            Description: replaceImageRefs(f.description),
            Comments: (f.comment?.comments || []).map(c => ({
              id: c.id,
              author: c.author?.displayName,
              authorAccountId: c.author?.accountId,
              body: replaceImageRefs(c.body),
              created: c.created,
            })),
            Attachments: allAttachments.map((a) => {
              const file = fileMap.get(a.id);
              const downloadError = errorMap.get(a.id) || null;
              let error;
              if (file) {
                error = file.conversionError || null;            // success (may carry a conversion error)
              } else if (downloadError) {
                error = downloadError;                            // real download/convert failure — original reason (C3)
              } else if (!downloadIds.has(a.id)) {
                error = `Attachment not downloaded: skipped because it is not among the ${attachmentDownloadLimit} newest attachments (jira/attachment_download_limit=${attachmentDownloadLimit}). Use jira_get_attachment(issue_key="${data.key}", attachment_id="${a.id}") to fetch it on demand.`;
              } else {
                error = 'Attachment download produced no result (unknown reason).'; // C2 backstop
              }
              return {
                id: a.id,
                filename: a.filename,
                mimeType: a.mimeType,
                size: a.size,
                created: a.created,
                localPath: file?.localPath || null,
                convertedPath: file?.convertedPath || null,
                error,
              };
            }),
          };

          const fileCount = fileMap.size;
          log('jira_get_issue', 'OK', `user=${user} ${issue_key} "${issue.Summary}" ${issue.Comments.length} comments ${fileCount} attachments`);
          return { content: [{ type: 'text', text: JSON.stringify(issue, null, 2) }] };
        } catch (err) {
          log('jira_get_issue', 'ERROR', `user=${user} ${issue_key} ${err.message}`);
          return {
            content: [{ type: 'text', text: `Jira get issue error: ${err.message}` }],
            isError: true,
          };
        }
      }
    );
  }

  // --- jira_add_comment ---
  if (enabled('jira_add_comment')) {
    server.tool(
      'jira_add_comment',
      [
        'Add a comment to a Jira issue. Supports replying to an existing comment by quoting it and mentioning the author.',
        'Use Jira wiki markup in body. To reply, pass reply_to_comment_id from jira_get_issue comments.',
        'Examples:',
        '  issue_key: "AI-1", body: "Zadanie zrealizowane, zmiany na branchu feature/ai-1"',
        '  issue_key: "AI-1", body: "Zgadzam się z propozycją.", reply_to_comment_id: "10042"',
      ].join('\n'),
      {
        user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
        issue_key: z.string().describe('Jira issue key, e.g. "AI-1"'),
        body: z.string().describe('Comment text (Jira wiki markup)'),
        reply_to_comment_id: z.string().optional().describe('ID of existing comment to reply to — will quote original and mention author'),
      },
      async ({ user, issue_key, body, reply_to_comment_id }) => {
        const auth = Buffer.from(`${config.user}:${config.token}`).toString('base64');
        const headers = {
          'Authorization': `Basic ${auth}`,
          'Accept': 'application/json',
          'Content-Type': 'application/json',
        };

        try {
          let finalBody = body;

          if (reply_to_comment_id) {
            const commentRes = await fetch(
              `${config.host}/rest/api/2/issue/${encodeURIComponent(issue_key)}/comment/${reply_to_comment_id}`,
              { headers: { 'Authorization': `Basic ${auth}`, 'Accept': 'application/json' } }
            );
            if (!commentRes.ok) {
              throw new Error(`Failed to fetch comment ${reply_to_comment_id}: HTTP ${commentRes.status}`);
            }
            const original = await commentRes.json();
            const accountId = original.author?.accountId;
            const originalBody = (original.body || '').slice(0, 200);
            finalBody = `[~accountId:${accountId}]\n{quote}${originalBody}{quote}\n\n${body}`;
          }

          const response = await fetch(
            `${config.host}/rest/api/2/issue/${encodeURIComponent(issue_key)}/comment`,
            {
              method: 'POST',
              headers,
              body: JSON.stringify({ body: finalBody }),
            }
          );

          if (!response.ok) {
            const text = await response.text();
            throw new Error(`HTTP ${response.status}: ${text}`);
          }

          const data = await response.json();
          const detail = reply_to_comment_id ? ` (reply to ${reply_to_comment_id})` : '';
          log('jira_add_comment', 'OK', `user=${user} ${issue_key} commentId=${data.id}${detail}`);
          return {
            content: [{ type: 'text', text: `Comment added to ${issue_key} (id: ${data.id})${detail}` }],
          };
        } catch (err) {
          log('jira_add_comment', 'ERROR', `user=${user} ${issue_key} ${err.message}`);
          return {
            content: [{ type: 'text', text: `Jira comment error: ${err.message}` }],
            isError: true,
          };
        }
      }
    );
  }

  // --- jira_transition_issue ---
  if (enabled('jira_transition_issue')) {
    server.tool(
      'jira_transition_issue',
      [
        'Change the status of a Jira issue by target status name (e.g. "In Progress", "Review", "Done").',
        'Internally resolves the transition ID from available transitions.',
        'If the requested status is not available, returns a list of valid transitions.',
        'Examples:',
        '  issue_key: "AI-1", status_name: "In Progress"',
        '  issue_key: "K3-542", status_name: "Review"',
      ].join('\n'),
      {
        user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
        issue_key: z.string().describe('Jira issue key, e.g. "AI-1"'),
        status_name: z.string().describe('Target status name, e.g. "In Progress", "Review", "Done"'),
      },
      async ({ user, issue_key, status_name }) => {
        const auth = Buffer.from(`${config.user}:${config.token}`).toString('base64');
        const headers = {
          'Authorization': `Basic ${auth}`,
          'Accept': 'application/json',
          'Content-Type': 'application/json',
        };

        try {
          const transRes = await fetch(
            `${config.host}/rest/api/2/issue/${encodeURIComponent(issue_key)}/transitions`,
            { headers: { 'Authorization': `Basic ${auth}`, 'Accept': 'application/json' } }
          );

          if (!transRes.ok) {
            const text = await transRes.text();
            throw new Error(`Failed to get transitions: HTTP ${transRes.status}: ${text}`);
          }

          const transData = await transRes.json();
          const transitions = transData.transitions || [];

          const target = transitions.find(
            t => t.name.toLowerCase() === status_name.toLowerCase()
              || t.to?.name?.toLowerCase() === status_name.toLowerCase()
          );

          if (!target) {
            const available = transitions.map(t => `"${t.name}" -> "${t.to?.name || '?'}"`).join(', ');
            throw new Error(
              `No transition matching "${status_name}" found. Available transitions: [${available}]`
            );
          }

          const response = await fetch(
            `${config.host}/rest/api/2/issue/${encodeURIComponent(issue_key)}/transitions`,
            {
              method: 'POST',
              headers,
              body: JSON.stringify({ transition: { id: target.id } }),
            }
          );

          if (!response.ok) {
            const text = await response.text();
            throw new Error(`HTTP ${response.status}: ${text}`);
          }

          log('jira_transition_issue', 'OK', `user=${user} ${issue_key} -> "${target.to?.name || status_name}" (transition: "${target.name}", id: ${target.id})`);
          return {
            content: [{ type: 'text', text: `Issue ${issue_key} transitioned to "${target.to?.name || status_name}" via "${target.name}"` }],
          };
        } catch (err) {
          log('jira_transition_issue', 'ERROR', `user=${user} ${issue_key} ${err.message}`);
          return {
            content: [{ type: 'text', text: `Jira transition error: ${err.message}` }],
            isError: true,
          };
        }
      }
    );
  }

  // --- jira_assign_issue ---
  if (enabled('jira_assign_issue')) {
    server.tool(
      'jira_assign_issue',
      [
        'Assign a Jira issue to a user by accountId, or use special values.',
        'Special values:',
        '  "reporter" — assigns to the issue reporter (auto-resolves accountId)',
        '  "unassigned" — removes the assignee',
        'You can get accountId from jira_get_issue (ReporterAccountId, AssigneeAccountId) or from comment authorAccountId.',
        'Examples:',
        '  issue_key: "AI-1", assignee: "reporter"',
        '  issue_key: "AI-1", assignee: "unassigned"',
        '  issue_key: "AI-1", assignee: "60d1f2e3a4b5c6d7e8f90123"',
      ].join('\n'),
      {
        user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
        issue_key: z.string().describe('Jira issue key, e.g. "AI-1"'),
        assignee: z.string().describe('accountId, or "reporter" to assign to reporter, or "unassigned" to clear'),
      },
      async ({ user, issue_key, assignee }) => {
        const auth = Buffer.from(`${config.user}:${config.token}`).toString('base64');
        const headers = {
          'Authorization': `Basic ${auth}`,
          'Accept': 'application/json',
          'Content-Type': 'application/json',
        };

        try {
          let accountId = null;

          if (assignee.toLowerCase() === 'unassigned') {
            accountId = null;
          } else if (assignee.toLowerCase() === 'reporter') {
            const issueRes = await fetch(
              `${config.host}/rest/api/3/issue/${encodeURIComponent(issue_key)}?fields=reporter`,
              { headers: { 'Authorization': `Basic ${auth}`, 'Accept': 'application/json' } }
            );
            if (!issueRes.ok) {
              const text = await issueRes.text();
              throw new Error(`Failed to fetch issue for reporter lookup: HTTP ${issueRes.status}: ${text}`);
            }
            const issueData = await issueRes.json();
            accountId = issueData.fields?.reporter?.accountId;
            if (!accountId) {
              throw new Error(`Issue ${issue_key} has no reporter accountId`);
            }
          } else {
            accountId = assignee;
          }

          const response = await fetch(
            `${config.host}/rest/api/3/issue/${encodeURIComponent(issue_key)}/assignee`,
            {
              method: 'PUT',
              headers,
              body: JSON.stringify({ accountId }),
            }
          );

          if (!response.ok) {
            const text = await response.text();
            throw new Error(`HTTP ${response.status}: ${text}`);
          }

          const detail = assignee.toLowerCase() === 'reporter'
            ? `reporter (${accountId})`
            : assignee.toLowerCase() === 'unassigned'
              ? 'unassigned'
              : accountId;

          log('jira_assign_issue', 'OK', `user=${user} ${issue_key} assignee=${detail}`);
          return {
            content: [{ type: 'text', text: `Issue ${issue_key} assigned to ${detail}` }],
          };
        } catch (err) {
          log('jira_assign_issue', 'ERROR', `user=${user} ${issue_key} ${err.message}`);
          return {
            content: [{ type: 'text', text: `Jira assign error: ${err.message}` }],
            isError: true,
          };
        }
      }
    );
  }

  // --- jira_attach_file ---
  if (enabled('jira_attach_file')) {
    server.tool(
      'jira_attach_file',
      [
        'Attach a local file to a Jira issue.',
        'Use after browser_take_screenshot to attach the saved screenshot.',
        'file_path must be inside /workspace/ (security restriction).',
        'Examples:',
        '  issue_key: "AI-1", file_path: "/workspace/artifacts/{ws}/{av}/{job_id}/screenshots/123-AI-1/1740000000000.png"',
      ].join('\n'),
      {
        user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
        issue_key: z.string().describe('Jira issue key, e.g. "AI-1"'),
        file_path: z.string().describe('Absolute local path to the file to attach (must be inside /workspace/)'),
        filename: z.string().optional().describe('Override filename shown in Jira. Defaults to basename of file_path.'),
      },
      async ({ user, issue_key, file_path, filename }) => {
        if (!file_path.startsWith('/workspace/')) {
          return {
            content: [{ type: 'text', text: 'Error: file_path must be inside /workspace/' }],
            isError: true,
          };
        }

        const auth = Buffer.from(`${config.user}:${config.token}`).toString('base64');
        const attachFilename = filename || basename(file_path);

        try {
          const fileBuffer = await readFile(file_path);
          const blob = new Blob([fileBuffer]);
          const form = new FormData();
          form.append('file', blob, attachFilename);

          const response = await fetch(
            `${config.host}/rest/api/3/issue/${encodeURIComponent(issue_key)}/attachments`,
            {
              method: 'POST',
              headers: {
                'Authorization': `Basic ${auth}`,
                'X-Atlassian-Token': 'no-check',
              },
              body: form,
            }
          );

          if (!response.ok) {
            const text = await response.text();
            throw new Error(`HTTP ${response.status}: ${text}`);
          }

          const data = await response.json();
          const att = Array.isArray(data) ? data[0] : data;
          log('jira_attach_file', 'OK', `user=${user} ${issue_key} filename=${att.filename} id=${att.id}`);
          return {
            content: [{ type: 'text', text: `Attached "${att.filename}" to ${issue_key} (attachment id: ${att.id})` }],
          };
        } catch (err) {
          log('jira_attach_file', 'ERROR', `user=${user} ${issue_key} ${err.message}`);
          return {
            content: [{ type: 'text', text: `Jira attach error: ${err.message}` }],
            isError: true,
          };
        }
      }
    );
  }

  // --- jira_create_issue ---
  const _createRateLimit = {
    count: 0,
    windowStart: Date.now(),
    limit: parseInt(cfg.create_issue_limit_per_hour || '50', 10),
  };

  function checkCreateRateLimit() {
    const now = Date.now();
    if (now - _createRateLimit.windowStart >= 3_600_000) {
      _createRateLimit.count = 0;
      _createRateLimit.windowStart = now;
    }
    if (_createRateLimit.count >= _createRateLimit.limit) {
      const resetIn = Math.ceil((3_600_000 - (now - _createRateLimit.windowStart)) / 60_000);
      throw new Error(`Rate limit exceeded: max ${_createRateLimit.limit} issues per hour. Resets in ~${resetIn} min.`);
    }
    _createRateLimit.count++;
  }

  if (enabled('jira_create_issue')) {
    server.tool(
      'jira_create_issue',
      [
        'Create a new Jira issue in a given project.',
        'Returns the key of the created issue (e.g. "AI-42").',
        `Rate limit: ${_createRateLimit.limit} issues per hour (config: jira/create_issue_limit_per_hour).`,
        'Examples:',
        '  project_key: "AI", issue_type: "Task", summary: "Nowa funkcja X"',
        '  project_key: "K3", issue_type: "Bug", summary: "Błąd cen", description: "...", priority: "High"',
      ].join('\n'),
      {
        user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
        project_key: z.string().describe('Jira project key, e.g. "AI", "K3"'),
        issue_type: z.string().describe('Issue type name, e.g. "Task", "Bug", "Story", "Subtask"'),
        summary: z.string().describe('Issue summary (title)'),
        description: z.string().optional().describe('Issue description (Jira wiki markup)'),
        assignee_account_id: z.string().optional().describe('accountId of the assignee'),
        priority: z.string().optional().describe('Priority name, e.g. "High", "Medium", "Low"'),
        parent_key: z.string().optional().describe('Parent issue key for subtasks, e.g. "AI-10"'),
        due_date: z.string().optional().describe('Due date in YYYY-MM-DD format, e.g. "2025-06-30"'),
        reporter_account_id: z.string().optional().describe('Reporter accountId (from jira_get_issue ReporterAccountId)'),
      },
      async ({ user, project_key, issue_type, summary, description, assignee_account_id, priority, parent_key, due_date, reporter_account_id }) => {
        const auth = Buffer.from(`${config.user}:${config.token}`).toString('base64');
        const headers = {
          'Authorization': `Basic ${auth}`,
          'Accept': 'application/json',
          'Content-Type': 'application/json',
        };

        try {
          checkCreateRateLimit();

          const fields = {
            project: { key: project_key },
            issuetype: { name: issue_type },
            summary,
          };
          if (description) fields.description = description;
          if (assignee_account_id) fields.assignee = { accountId: assignee_account_id };
          if (priority) fields.priority = { name: priority };
          if (parent_key) fields.parent = { key: parent_key };
          if (due_date) fields.duedate = due_date;
          if (reporter_account_id) fields.reporter = { accountId: reporter_account_id };

          const response = await fetch(
            `${config.host}/rest/api/2/issue`,
            { method: 'POST', headers, body: JSON.stringify({ fields }) }
          );

          if (!response.ok) {
            const text = await response.text();
            throw new Error(`HTTP ${response.status}: ${text}`);
          }

          const data = await response.json();
          log('jira_create_issue', 'OK', `user=${user} created ${data.key} in ${project_key} type=${issue_type} "${summary}"`);
          return {
            content: [{ type: 'text', text: JSON.stringify({ key: data.key, id: data.id, url: `${config.host}/browse/${data.key}` }, null, 2) }],
          };
        } catch (err) {
          log('jira_create_issue', 'ERROR', `user=${user} ${project_key} ${err.message}`);
          return {
            content: [{ type: 'text', text: `Jira create issue error: ${err.message}` }],
            isError: true,
          };
        }
      }
    );
  }

  // --- jira_update_issue ---
  if (enabled('jira_update_issue')) {
    server.tool(
      'jira_update_issue',
      [
        'Update fields of an existing Jira issue.',
        'Pass only the fields you want to change — omitted fields are left unchanged.',
        'At least one field must be provided.',
        'Examples:',
        '  issue_key: "AI-1", summary: "Nowy tytuł"',
        '  issue_key: "K3-42", description: "Nowy opis", priority: "High", due_date: "2025-06-30"',
        '  issue_key: "AI-1", labels: ["bug", "frontend"]',
        '  issue_key: "AI-5", parent_key: "AI-1"  (make subtask of AI-1)',
      ].join('\n'),
      {
        user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
        issue_key: z.string().describe('Jira issue key, e.g. "AI-1"'),
        summary: z.string().optional().describe('New summary (title)'),
        description: z.string().optional().describe('New description (Jira wiki markup)'),
        priority: z.string().optional().describe('Priority name, e.g. "High", "Medium", "Low"'),
        assignee_account_id: z.string().optional().describe('accountId, or "unassigned" to clear'),
        reporter_account_id: z.string().optional().describe('Reporter accountId'),
        labels: z.array(z.string()).optional().describe('Replace all labels on the issue'),
        due_date: z.string().optional().describe('Due date in YYYY-MM-DD format, e.g. "2025-06-30"'),
        parent_key: z.string().optional().describe('Parent issue key, e.g. "AI-10" (make this a subtask)'),
      },
      async ({ user, issue_key, summary, description, priority, assignee_account_id, reporter_account_id, labels, due_date, parent_key }) => {
        const auth = Buffer.from(`${config.user}:${config.token}`).toString('base64');
        const headers = {
          'Authorization': `Basic ${auth}`,
          'Accept': 'application/json',
          'Content-Type': 'application/json',
        };

        try {
          const fields = {};
          if (summary) fields.summary = summary;
          if (description) fields.description = description;
          if (priority) fields.priority = { name: priority };
          if (assignee_account_id) {
            fields.assignee = assignee_account_id === 'unassigned' ? null : { accountId: assignee_account_id };
          }
          if (reporter_account_id) fields.reporter = { accountId: reporter_account_id };
          if (labels) fields.labels = labels;
          if (due_date) fields.duedate = due_date;
          if (parent_key) fields.parent = { key: parent_key };

          if (Object.keys(fields).length === 0) {
            return {
              content: [{ type: 'text', text: 'Error: at least one field to update must be provided' }],
              isError: true,
            };
          }

          const response = await fetch(
            `${config.host}/rest/api/2/issue/${encodeURIComponent(issue_key)}`,
            { method: 'PUT', headers, body: JSON.stringify({ fields }) }
          );

          if (!response.ok) {
            const text = await response.text();
            throw new Error(`HTTP ${response.status}: ${text}`);
          }

          const updated = Object.keys(fields).join(', ');
          log('jira_update_issue', 'OK', `user=${user} ${issue_key} updated: ${updated}`);
          return {
            content: [{ type: 'text', text: `Issue ${issue_key} updated: ${updated}` }],
          };
        } catch (err) {
          log('jira_update_issue', 'ERROR', `user=${user} ${issue_key} ${err.message}`);
          return {
            content: [{ type: 'text', text: `Jira update issue error: ${err.message}` }],
            isError: true,
          };
        }
      }
    );
  }

  // --- jira_get_attachment (opt-in: disabled by default; enable via `agento tool:enable jira_get_attachment`) ---
  if (enabled('jira_get_attachment')) {
    server.tool(
      'jira_get_attachment',
      [
        'Download a SINGLE Jira attachment by its attachment ID, on demand.',
        'Unlike jira_get_issue (which auto-downloads only the newest N attachments, per jira/attachment_download_limit), this fetches ANY attachment regardless of that limit.',
        'Use it for an attachment that jira_get_issue listed with a "skipped because ... limit" error.',
        'Returns the local path to the downloaded file (and convertedPath for auto-converted binaries like PDF/XLSX).',
        'Examples:',
        '  issue_key: "AI-1", attachment_id: "10500"',
      ].join('\n'),
      {
        user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
        issue_key: z.string().regex(JIRA_KEY_RE, 'must be a Jira issue key like "AI-1"').describe('Jira issue key the attachment belongs to, e.g. "AI-1" — determines the artifacts subfolder'),
        attachment_id: z.string().describe('Jira attachment ID (from jira_get_issue Attachments[].id)'),
      },
      async ({ user, issue_key, attachment_id }) => {
        // Defense-in-depth: issue_key flows into a filesystem path (join below). Reject anything that is
        // not a clean Jira key so a value like "../../x" can never escape the artifacts subtree — even
        // from a non-validating MCP client that bypasses the Zod regex above.
        if (!JIRA_KEY_RE.test(issue_key)) {
          log('jira_get_attachment', 'ERROR', `user=${user} rejected invalid issue_key="${issue_key}"`);
          return {
            content: [{ type: 'text', text: `Jira get attachment error: invalid issue_key "${issue_key}" (expected a Jira key like "AI-1")` }],
            isError: true,
          };
        }

        const auth = Buffer.from(`${config.user}:${config.token}`).toString('base64');
        const authHeader = { 'Authorization': `Basic ${auth}`, 'Accept': 'application/json' };

        try {
          const metaRes = await fetch(
            `${config.host}/rest/api/3/attachment/${encodeURIComponent(attachment_id)}`,
            { headers: authHeader }
          );
          if (!metaRes.ok) {
            const text = await metaRes.text();
            throw new Error(`HTTP ${metaRes.status}: ${text}`);
          }
          const meta = await metaRes.json();

          if (!fileManager) {
            throw new Error('File manager unavailable');
          }

          const dir = join(artifactsDir, 'jira', issue_key);
          const result = await fileManager.download(meta.content, meta.filename, {
            headers: authHeader,
            dir,
            maxSize: meta.size,
          });

          if (result.skipped) {
            log('jira_get_attachment', 'WARN', `user=${user} ${issue_key} att=${attachment_id} skipped: ${result.skipReason}`);
            return {
              content: [{ type: 'text', text: `Jira get attachment error: ${result.skipReason}` }],
              isError: true,
            };
          }

          const out = {
            id: attachment_id,
            filename: meta.filename,
            mimeType: meta.mimeType,
            size: meta.size,
            localPath: result.localPath,
            convertedPath: result.convertedPath || null,
            error: result.conversionError || null,
          };
          log('jira_get_attachment', 'OK', `user=${user} ${issue_key} att=${attachment_id} -> ${result.localPath}`);
          return { content: [{ type: 'text', text: JSON.stringify(out, null, 2) }] };
        } catch (err) {
          log('jira_get_attachment', 'ERROR', `user=${user} ${issue_key} att=${attachment_id} ${err.message}`);
          return {
            content: [{ type: 'text', text: `Jira get attachment error: ${err.message}` }],
            isError: true,
          };
        }
      }
    );
  }
}
