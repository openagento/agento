import { z } from 'zod';
import { stat, lstat, realpath, readFile, writeFile, mkdir } from 'node:fs/promises';
import { basename, join, resolve, sep } from 'node:path';
import { createGraphAuth } from './graph-auth.js';
import { parseDmarcVerdict } from './api-handlers.js';

const GRAPH_BASE = 'https://graph.microsoft.com/v1.0';

const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
const SIMPLE_ATTACH_LIMIT = 3 * 1024 * 1024; // <3 MB → simple POST; ≥3 MB → upload session
const UPLOAD_CHUNK = 320 * 1024 * 10; // 3.2 MB, a 320 KB multiple, <4 MB (Graph upload requirement)

// Resolve `p` to its REAL path (following `..` and symlinks) and assert it stays inside /workspace/.
// This code runs in the toolbox (the secrets container), so a path that escapes /workspace lets the
// untrusted agent make the toolbox read a host/secret file and email it out — a prefix-only
// startsWith('/workspace/') check is INSUFFICIENT (/workspace/../etc/passwd passes it, and a symlink
// under /workspace can point outside). Resolving the real path closes both. Re-resolving /workspace per
// call is negligible (≤10 files). Throws (never returns) when the path is missing or escapes — callers
// turn that into an isError before any Graph call. Shared by validateAttachments (file paths), the
// download write (artifactsDir), and the read-time re-check in attachAndSendDraft.
async function realpathWithin(p) {
  const wsReal = await realpath('/workspace');
  const real = await realpath(p);
  if (real !== wsReal && !real.startsWith(wsReal + sep)) {
    throw new Error(`path "${p}" escapes /workspace`);
  }
  return real;
}

// Create `dir` (which must be /workspace or under it) WITHOUT ever following a symlink. mkdir's
// `recursive: true` follows an existing intermediate symlink, so if the agent pre-creates a component
// such as /workspace/artifacts as a symlink to outside, a plain recursive mkdir would create directories
// OUTSIDE /workspace before any containment check runs. Anchor on realpath('/workspace') and create each
// missing segment one at a time, rejecting any existing segment that is a symlink or non-directory
// (lstat) — so no symlink is traversed and nothing is created outside the real workspace. Returns the
// resolved, contained directory. Throws on escape/symlink; callers turn that into an isError (no write).
async function mkdirWithinWorkspace(dir) {
  const wsReal = await realpath('/workspace');
  if (dir !== '/workspace' && !dir.startsWith('/workspace/')) {
    throw new Error(`path "${dir}" escapes /workspace`);
  }
  const rel = dir === '/workspace' ? '' : dir.slice('/workspace/'.length);
  let current = wsReal;
  for (const seg of rel.split('/').filter(Boolean)) {
    const next = join(current, seg);
    let info = null;
    try {
      info = await lstat(next);
    } catch (err) {
      if (err.code !== 'ENOENT') throw err;
    }
    if (info) {
      if (info.isSymbolicLink() || !info.isDirectory()) {
        throw new Error(`path component "${next}" is not a real directory`);
      }
    } else {
      try {
        await mkdir(next); // non-recursive: parent is a verified real dir, so this cannot follow a symlink
      } catch (err) {
        if (err.code !== 'EEXIST') throw err;
        // Lost a benign race with a concurrent worker creating the same dir — re-check it is a real dir.
        const raced = await lstat(next);
        if (raced.isSymbolicLink() || !raced.isDirectory()) {
          throw new Error(`path component "${next}" is not a real directory`);
        }
      }
    }
    current = next;
  }
  return current;
}

// Validate agent-supplied attachment paths BEFORE any Graph call. Hardened replica of core/email.js's
// validateAttachments (NOT imported — inter-module dependency): adds realpath
// traversal/symlink containment because this runs in the secrets container. Returns { error, records }
// with records = [{ realPath, name, size }]; realPath is the resolved, contained path used for ALL later
// I/O so the validated path is exactly the read path. First failing path → { error, records: [] }.
async function validateAttachments(paths) {
  const records = [];
  for (const p of paths) {
    if (!p.startsWith('/workspace/')) {
      return { error: `Error: attachment path "${p}" must be inside /workspace/`, records: [] };
    }
    let real;
    try {
      real = await realpathWithin(p);
    } catch (err) {
      // Not-found (realpath throws ENOENT) and escape both land here — never reveal which.
      if (err.message && err.message.includes('escapes /workspace')) {
        return { error: `Error: attachment path "${p}" escapes /workspace`, records: [] };
      }
      return { error: `Error: attachment "${p}" not found (${err.code || err.message})`, records: [] };
    }
    let info;
    try {
      info = await stat(real);
    } catch (err) {
      return { error: `Error: attachment "${p}" not found (${err.code || err.message})`, records: [] };
    }
    if (!info.isFile()) {
      return { error: `Error: attachment "${p}" is not a regular file`, records: [] };
    }
    if (info.size > MAX_ATTACHMENT_BYTES) {
      return { error: `Error: attachment "${p}" is ${info.size} bytes; exceeds 25 MB cap`, records: [] };
    }
    records.push({ realPath: real, name: basename(p), size: info.size });
  }
  return { error: null, records };
}

// Host guard for Graph upload-session URLs (AC4). The chunked PUT carries the raw attachment bytes with
// NO Authorization header (the session URL is the capability), so a Graph-returned URL pointing at a
// foreign host would exfiltrate bytes. Accept ONLY https + outlook.office.com (or a subdomain) with no
// embedded credentials. Any parse error ⇒ false (fail-closed). Exported for direct unit testing.
export function isOutlookUploadUrl(u) {
  try {
    const url = new URL(u);
    // Microsoft Graph returns the attachment upload-session URL on outlook.office365.com (verified
    // against the live tenant), while older/other paths use outlook.office.com. Trust both official
    // Microsoft 365 hosts (apex + subdomains). Without office365.com every attachment >=3 MB — which
    // takes the createUploadSession path — fails with "untrusted upload URL".
    const host = url.hostname;
    const trustedHost =
      host === 'outlook.office.com' || host.endsWith('.outlook.office.com') ||
      host === 'outlook.office365.com' || host.endsWith('.outlook.office365.com');
    return (
      url.protocol === 'https:' &&
      trustedHost &&
      url.username === '' &&
      url.password === ''
    );
  } catch {
    return false;
  }
}

// Map a Graph attachment @odata.type to a short kind. Default 'file'.
function odataAttachmentType(t) {
  if (t === '#microsoft.graph.itemAttachment') return 'item';
  if (t === '#microsoft.graph.referenceAttachment') return 'reference';
  return 'file';
}

function loadWhitelist(moduleConfigs) {
  return (moduleConfigs?.core?.email_whitelist || '')
    .split(',')
    .map((p) => p.trim().toLowerCase())
    .filter(Boolean);
}

// Every address that IS this mailbox: the primary UPN plus outlook/mailbox_aliases. Mail delivered to an
// alias keeps that alias in the reply-all recipient list; gating it against core/email_whitelist would make
// the agent report (or block) itself. Comparison is trimmed + lowercased on both sides.
export function buildSelfAddresses(mailbox, aliasesCsv) {
  const out = new Set();
  const primary = (mailbox || '').trim().toLowerCase();
  if (primary) out.add(primary);
  for (const a of String(aliasesCsv || '').split(',')) {
    const v = a.trim().toLowerCase();
    if (v) out.add(v);
  }
  return out;
}

// Inbound allow-list (outlook/allowed_senders) — same comma-separated glob format used by the publisher
// gate (channel.py). Drives the read-tool restriction below.
function loadAllowedSenders(cfg) {
  return (cfg?.allowed_senders || '')
    .split(',')
    .map((p) => p.trim().toLowerCase())
    .filter(Boolean);
}

// Config booleans arrive as a real bool (config.json) or a string (DB/ENV). Missing/empty -> default.
function parseBool(value, dflt) {
  if (value === undefined || value === null || value === '') return dflt;
  if (typeof value === 'boolean') return value;
  return ['1', 'true', 'yes', 'on'].includes(String(value).trim().toLowerCase());
}

// outlook/reply_policy governs how outlook_reply handles a reply-all recipient not in core/email_whitelist.
// 'block' → block the whole send (mirrors outlook_send_mail). Anything else (incl. missing/empty) → the
// default 'remove' → drop the blocked recipient(s) and send to the rest. Either way only whitelisted
// addresses ever receive mail.
function resolveReplyPolicy(value) {
  return String(value ?? '').trim().toLowerCase() === 'block' ? 'block' : 'remove';
}

// Case-insensitive dedupe preserving first-seen original casing (email addresses are case-insensitive).
function dedupeCI(addrs) {
  return [...new Map(addrs.filter(Boolean).map((a) => [a.toLowerCase(), a])).values()];
}

// On a non-ok Graph response, drain+discard the body (it can carry mailbox/tenant identifiers we must
// NOT surface to the agent — same policy as api-handlers.js) and throw a STATUS-ONLY error. The tool
// catch blocks log this server-side and return it to the agent already sanitized.
async function ensureOk(res) {
  if (!res.ok) {
    await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status}`);
  }
}

// `matchesWhitelist` is the shared toolbox matcher (one implementation, in lockstep with the
// Python twin). It arrives through the registration context because a module cannot import
// framework code by path (see config-loader.js TOOLBOX_HELPERS).
export function register(server, { log, moduleConfigs, isToolEnabled, graphAuthFactory, artifactsDir, db, jobId, agentViewId, matchesWhitelist }) {
  const cfg = moduleConfigs?.outlook || {};
  const auth = (graphAuthFactory || createGraphAuth)(cfg);
  const whitelist = loadWhitelist(moduleConfigs);
  const replyPolicy = resolveReplyPolicy(cfg.reply_policy);
  const mailboxAliases = cfg.mailbox_aliases;

  // Bot-to-bot loop suppression needs NO outbound stamping: an inbound message is treated as
  // agent-authored when its (DMARC-verified) From is one of the deployment's fleet mailboxes — the set
  // auto-derived from the agent_views (every OTHER outlook-enabled view's resolved mailbox), computed in
  // the delta handler (api-handlers.js deriveFleetMailboxes / isAgentSender). Graph makes
  // internetMessageHeaders read-only after create, so a per-message HMAC header can't be set on a reply
  // draft — address matching is the reliable, Graph-header-free signal.

  // S3 READ RESTRICTION: when `restrict_read_to_allowed_senders` is on (DEFAULT true), the read tools
  // (get_message / get_attachment) surface a message only if it passes the SAME gate as the inbound
  // publisher (channel.py): sender on outlook/allowed_senders AND a DMARC `pass`. The From header is
  // forgeable, so an allow-listed sender alone is NOT enough — DMARC is the cryptographic proof; without
  // it a spoofed allow-listed From on a DMARC-failing mail would be readable (a prompt-injection vector).
  // The verdict is the immutable receipt-time Authentication-Results header (parseDmarcVerdict), checked
  // FAIL-CLOSED: no verifiable `pass` ⇒ not surfaced. Empty allowed_senders = block all. Disabling
  // restrict_read_to_allowed_senders bypasses BOTH checks (lets the agent read any mail) — a documented
  // security risk. This runs IN ADDITION to the current-job binding below.
  const allowedSenders = loadAllowedSenders(cfg);
  const restrictRead = parseBool(cfg.restrict_read_to_allowed_senders, true);
  const surfaceAllowed = (addr, headers) =>
    !restrictRead || (matchesWhitelist(addr || '', allowedSenders) && parseDmarcVerdict(headers) === 'pass');

  // THREAD READ (opt-in, DEFAULT OFF): let a headless job READ earlier messages/attachments of the SAME
  // Outlook conversation that triggered it. Extends the READ scope only — ACTIONS (reply/mark_processed)
  // stay bound to the triggering message. The conversation is always derived from the trusted trigger
  // message; the agent never supplies a conversationId or a foreign message id (see resolveThreadBinding).
  // The SINGLE gate is the tool switch `tools/outlook_list_thread/is_enabled` (off by default, like every
  // tool): it registers the tool AND widens the read scope, so admin has one decision, not two.
  const threadReadMax = (() => {
    const n = parseInt(cfg.thread_read_max_messages, 10);
    return Number.isFinite(n) && n > 0 ? Math.min(n, 200) : 50;
  })();

  // CURRENT-JOB READ BINDING: privacy-by-construction for headless email jobs. With no enumeration tool,
  // the only remaining read vector is a leaked opaque message id; bind get_message/get_attachment to the
  // id that TRIGGERED this job so a foreign id cannot be read. Resolved once per session (this promise is
  // register()-scoped — NEVER module scope). jobId null (interactive `agento run`) → binding disabled
  // (operator escape hatch). A jobId that resolves no row scoped to THIS agent_view + Outlook source →
  // fail closed (serve nothing). reference_id is `{slug}::{message_id}`; the bare id is the tail.
  let bindingPromise;
  const resolveBinding = () => {
    if (!bindingPromise) {
      bindingPromise = (async () => {
        try {
          const [rows] = await db.getCronPool().query(
            "SELECT reference_id FROM job WHERE id = ? AND agent_view_id = ? AND source = 'outlook'",
            [jobId, agentViewId]
          );
          if (!rows || rows.length === 0) return { bound: false };
          return { bound: true, messageId: String(rows[0].reference_id || '').split('::').pop() };
        } catch (err) {
          log('outlook_read_binding', 'ERROR', `job binding lookup failed: ${err.message}`);
          return { bound: false };
        }
      })();
    }
    return bindingPromise;
  };
  // ACTION binding — governs outlook_reply / outlook_mark_processed. UNCHANGED by thread read: an action
  // may only ever target the triggering message. jobId null → allow (interactive escape hatch).
  const jobActionBindingAllows = async (requestedId) => {
    if (jobId === null || jobId === undefined) return true;
    const b = await resolveBinding();
    return b.bound && b.messageId === requestedId;
  };

  // THREAD BINDING — an authorized, sanitized view of the trigger message's conversation, built ONLY from
  // the trusted trigger (never from an agent-supplied id). resolveBinding → trigger message → its
  // conversationId → enumerate that conversation → authorize EACH message (trusted Sent-Items outbound, or
  // the same inbound gate as surfaceAllowed) → allowedMessageIds. Cached per session (register()-scoped
  // promise, NEVER module scope) so list_thread/get_message/get_attachment enumerate Graph at most once.
  // Fail-closed: no binding, no conversationId, or any Graph error ⇒ empty (bound:false).
  let threadBindingPromise;
  const emptyThread = () => ({ bound: false, allowedMessageIds: new Set(), messages: [], truncated: false });
  // A transient-failure variant: the same fail-closed empty view, but flagged `incomplete` so
  // resolveThreadBinding does NOT keep it in the session cache — the next call re-enumerates against Graph
  // instead of replaying the failure forever.
  const failedThread = () => ({ ...emptyThread(), incomplete: true });
  const resolveThreadBinding = () => {
    if (!threadBindingPromise) {
      const pending = (async () => {
        const b = await resolveBinding();
        if (!b.bound) return emptyThread();
        const mailbox = auth.getMailboxUserId();
        // 1. Trusted root: the trigger message's OWN conversationId (never an agent-supplied id).
        let conversationId;
        try {
          const tRes = await graphFetch(
            `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(b.messageId)}?$select=id,conversationId`
          );
          await ensureOk(tRes);
          conversationId = (await tRes.json())?.conversationId;
        } catch (err) {
          log('outlook_list_thread', 'ERROR', `mailbox=${mailbox} thread root resolution failed: ${err.message}`);
          return failedThread();
        }
        if (!conversationId) {
          log('outlook_list_thread', 'ERROR', `mailbox=${mailbox} trigger message has no conversationId`);
          return emptyThread();
        }
        // 2. Enumerate the conversation (newest-first by Graph default; $top caps to the newest N) and
        //    resolve the Sent Items folder id (proves a message physically lives in Sent — the outbound
        //    trust signal; a forged From alone is NOT enough).
        let listed;
        let sentItemsId = null;
        try {
          sentItemsId = await graphGetSentItemsId(mailbox);
          listed = await graphListConversation(mailbox, conversationId, threadReadMax);
        } catch (err) {
          log('outlook_list_thread', 'ERROR', `mailbox=${mailbox} thread enumeration failed: ${err.message}`);
          return failedThread();
        }
        // 3. Authorize each message; keep only allowed ones. Chronological oldest→newest for the agent.
        const chronological = [...listed.messages].sort(
          (m1, m2) =>
            new Date(m1.receivedDateTime || m1.sentDateTime || 0) -
            new Date(m2.receivedDateTime || m2.sentDateTime || 0)
        );
        // `incomplete` turns true on any transient Graph error below (header hydration / attachment
        // metadata). It is surfaced to the agent AND keeps the result out of the session cache, so a retry
        // can recover the full picture instead of serving a stale, silently-lossy view.
        let incomplete = false;
        let rejected = 0;
        const allowed = [];
        for (const m of chronological) {
          const trustedOutbound = Boolean(sentItemsId) && m.parentFolderId === sentItemsId;
          if (trustedOutbound) {
            allowed.push({ m, outbound: true });
            continue;
          }
          // Inbound → the same gate as surfaceAllowed. Graph's collection response sometimes OMITS
          // internetMessageHeaders even when $select asks for it; hydrate them per-message before deciding
          // (exactly like the delta poller in api-handlers.js), or a valid allow-listed message whose header
          // response passes DMARC would be silently dropped. A hydration failure is transient: fail closed
          // for THIS message AND flag the whole result incomplete so it is not cached.
          let headers = m.internetMessageHeaders;
          if (!Array.isArray(headers)) {
            try {
              const hr = await graphFetch(
                `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(m.id)}?$select=internetMessageHeaders`
              );
              await ensureOk(hr);
              headers = (await hr.json())?.internetMessageHeaders;
            } catch (err) {
              log('outlook_list_thread', 'WARN', `mailbox=${mailbox} header hydration failed for a thread message: ${err.message}`);
              headers = undefined;
              incomplete = true;
            }
          }
          if (surfaceAllowed(m.from?.emailAddress?.address, headers)) {
            allowed.push({ m, outbound: false });
          } else {
            rejected += 1;
          }
        }
        // 4. Attachment METADATA only, and ONLY for allowed messages that have attachments (never for a
        //    rejected message; never contentBytes). A transient metadata error must NOT be frozen into the
        //    session cache as an empty list (the agent could never recover the file): mark THIS message
        //    attachments_incomplete and flag the whole result incomplete so it is not cached and a later
        //    call can fetch the real metadata once Graph is available again.
        const messages = [];
        for (const { m, outbound } of allowed) {
          let attachments = [];
          let attachmentsIncomplete = false;
          if (m.hasAttachments) {
            try {
              const ar = await graphFetch(
                `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(m.id)}/attachments?$select=id,name,contentType,size`
              );
              await ensureOk(ar);
              const ad = await ar.json();
              attachments = (ad.value || []).map((a) => ({
                attachment_id: a.id,
                name: a.name,
                content_type: a.contentType,
                size: a.size,
              }));
            } catch (err) {
              log('outlook_list_thread', 'WARN', `mailbox=${mailbox} attachment metadata fetch failed: ${err.message}`);
              attachments = [];
              attachmentsIncomplete = true;
              incomplete = true;
            }
          }
          messages.push({
            message_id: m.id,
            direction: outbound ? 'outbound' : 'inbound',
            from: m.from?.emailAddress?.address || null,
            received_at: m.receivedDateTime || m.sentDateTime || null,
            subject: m.subject,
            preview: m.bodyPreview,
            has_attachments: Boolean(m.hasAttachments),
            attachments,
            ...(attachmentsIncomplete ? { attachments_incomplete: true } : {}),
          });
        }
        log(
          'outlook_list_thread',
          'OK',
          `mailbox=${mailbox} discovered=${listed.messages.length} allowed=${allowed.length} rejected=${rejected} truncated=${listed.truncated} incomplete=${incomplete}`
        );
        return {
          bound: true,
          conversationId,
          allowedMessageIds: new Set(allowed.map(({ m }) => m.id)),
          messages,
          truncated: listed.truncated,
          incomplete,
        };
      })();
      // Cache only DEFINITIVE results. A result flagged `incomplete` (a transient Graph error while
      // enumerating, hydrating headers, or listing attachment metadata) — or an outright rejection — is
      // dropped from the cache so a later call can recover instead of serving the failure forever. A bound
      // thread, or a real "no conversation" deny, stays cached (enumerated at most once per session).
      pending.then(
        (t) => { if (threadBindingPromise === pending && (!t || t.incomplete)) threadBindingPromise = undefined; },
        () => { if (threadBindingPromise === pending) threadBindingPromise = undefined; }
      );
      threadBindingPromise = pending;
    }
    return threadBindingPromise;
  };

  // READ classification — governs outlook_get_message / outlook_get_attachment. Returns the reason a read
  // is allowed so the caller can decide whether to re-run the inbound surface gate: a 'thread' message was
  // ALREADY authorized while building the binding (including trusted outbound, which surfaceAllowed would
  // wrongly reject), so it must NOT be re-gated. SECURITY: membership is tested against allowedMessageIds
  // WITHOUT a Graph GET of `requestedId` — a foreign opaque id is refused before its content is fetched.
  const classifyRead = async (requestedId) => {
    if (jobId === null || jobId === undefined) return { allowed: true, source: 'interactive' };
    const b = await resolveBinding();
    if (!b.bound) return { allowed: false, source: null };
    if (b.messageId === requestedId) return { allowed: true, source: 'trigger' };
    if (!enabled('outlook_list_thread')) return { allowed: false, source: null };
    const thread = await resolveThreadBinding();
    if (thread.bound && thread.allowedMessageIds.has(requestedId)) return { allowed: true, source: 'thread' };
    return { allowed: false, source: null };
  };

  // Per-tool opt-in gate. At startup (registerModuleRestApis) isToolEnabled is undefined and the server
  // is a no-op stub, so registering is harmless; at session time a disabled tool is skipped entirely.
  const enabled = (name) => !isToolEnabled || isToolEnabled(name);

  async function graphFetch(p, options = {}) {
    const token = await auth.getToken();
    return fetch(`${GRAPH_BASE}${p}`, {
      ...options,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...options.headers },
    });
  }

  // Resolve the mailbox's Sent Items folder id — the proof-of-origin for outbound thread messages (a
  // message physically in Sent Items was really sent by this mailbox; a forged `From` is not enough).
  async function graphGetSentItemsId(mailbox) {
    const res = await graphFetch(`/users/${encodeURIComponent(mailbox)}/mailFolders/sentitems?$select=id`);
    await ensureOk(res);
    return (await res.json())?.id || null;
  }

  // Enumerate the messages of ONE conversation (the trusted conversationId). Graph returns /messages
  // newest-first by default, so $top=max yields the newest `max`; an @odata.nextLink means older messages
  // were dropped (truncated). Only lightweight metadata is selected — never body/contentBytes. The
  // conversationId comes from the trigger message, never from the agent; single quotes are still doubled
  // for OData-literal safety.
  async function graphListConversation(mailbox, conversationId, max) {
    const literal = String(conversationId).replace(/'/g, "''");
    const filter = encodeURIComponent(`conversationId eq '${literal}'`);
    const select =
      'id,conversationId,subject,from,receivedDateTime,sentDateTime,hasAttachments,bodyPreview,parentFolderId,internetMessageHeaders';
    const res = await graphFetch(
      `/users/${encodeURIComponent(mailbox)}/messages?$filter=${filter}&$select=${select}&$top=${max}`
    );
    await ensureOk(res);
    const data = await res.json();
    return { messages: data.value || [], truncated: Boolean(data['@odata.nextLink']) };
  }

  // Attach validated files to a draft and send it. Defined INSIDE register() so it closes over the
  // session-scoped graphFetch (like graphFetch / surfaceAllowed / notConfigured) — never a module-level
  // function reading per-session auth from module scope (that is the cross-session contamination pattern
  // the rules forbid). Shared by the reply-all and send_mail draft paths; the only per-tool difference is
  // who created the draft. On ANY failure after the draft exists, best-effort DELETE the draft (no orphan)
  // then re-throw to the caller's outer catch.
  async function attachAndSendDraft(mailbox, draftId, records, recipientsOverride = null) {
    try {
      // reply_policy=remove: overwrite the draft's recipients with the whitelisted subset BEFORE
      // attaching/sending (createReplyAll auto-populated the full set; we drop the blocked ones). Runs
      // inside this try so the draft is DELETEd on any failure — no orphaned draft in Drafts.
      // SECURITY (load-bearing): Graph PATCH on a message REPLACES toRecipients/ccRecipients wholesale
      // (it does not merge/append), so the auto-populated blocked addresses are fully overwritten by the
      // whitelisted subset. Keep it a full replace — never a partial/merge update — or a blocked address
      // could survive into the send.
      if (recipientsOverride) {
        const pr = await graphFetch(
          `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(draftId)}`,
          { method: 'PATCH', body: JSON.stringify({ toRecipients: recipientsOverride.to, ccRecipients: recipientsOverride.cc }) }
        );
        await ensureOk(pr);
      }
      for (const { realPath, name } of records) {
        // Re-validate at read time (close the TOCTOU between early validation and upload): re-resolve +
        // re-assert /workspace containment (catches a post-validation symlink/file swap; throws → cleanup
        // runs), read from the FRESH resolved path, and use the ACTUAL byte length (not the stale
        // validated size) for the branch decision and the Graph size/Content-Range.
        const fresh = await realpathWithin(realPath);
        const bytes = await readFile(fresh);
        if (bytes.length > MAX_ATTACHMENT_BYTES) {
          throw new Error(`attachment "${name}" is ${bytes.length} bytes; exceeds 25 MB cap`);
        }
        if (bytes.length < SIMPLE_ATTACH_LIMIT) {
          // <3 MB → simple POST. (createUploadSession REJECTS files <3 MB with
          // ErrorAttachmentSizeShouldNotBeLessThanMinimumSize, so this branch is mandatory.)
          const ar = await graphFetch(
            `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(draftId)}/attachments`,
            {
              method: 'POST',
              body: JSON.stringify({
                '@odata.type': '#microsoft.graph.fileAttachment',
                name,
                contentBytes: bytes.toString('base64'),
              }),
            }
          );
          await ensureOk(ar);
        } else {
          // ≥3 MB → upload session + chunked PUT.
          const sr = await graphFetch(
            `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(draftId)}/attachments/createUploadSession`,
            {
              method: 'POST',
              body: JSON.stringify({ AttachmentItem: { attachmentType: 'file', name, size: bytes.length } }),
            }
          );
          await ensureOk(sr);
          const uploadUrl = (await sr.json())?.uploadUrl;
          if (!isOutlookUploadUrl(uploadUrl)) throw new Error('untrusted upload URL');
          for (let start = 0; start < bytes.length; start += UPLOAD_CHUNK) {
            const end = Math.min(start + UPLOAD_CHUNK, bytes.length) - 1;
            const chunk = bytes.subarray(start, end + 1);
            // RAW fetch (not graphFetch) so it carries NO Authorization header (the session URL is the
            // capability), its own Content-Range, and Content-Type: application/octet-stream (required by
            // Graph on upload PUTs). redirect:'manual' so native fetch can't follow a 3xx that would
            // forward raw bytes to a host the guard never checked; ensureOk then rejects the opaque
            // redirect / non-2xx.
            const pr = await fetch(uploadUrl, {
              method: 'PUT',
              redirect: 'manual',
              headers: {
                'Content-Length': String(chunk.length),
                'Content-Range': `bytes ${start}-${end}/${bytes.length}`,
                'Content-Type': 'application/octet-stream',
              },
              body: chunk,
            });
            await ensureOk(pr);
          }
        }
      }
      // Send returns 202 Accepted; ensureOk's res.ok covers all 2xx. Graph auto-saves to Sent Items.
      const sendRes = await graphFetch(
        `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(draftId)}/send`,
        { method: 'POST' }
      );
      await ensureOk(sendRes);
    } catch (err) {
      // Best-effort cleanup so a half-built draft never lingers in Drafts; swallow its own error.
      try {
        await graphFetch(
          `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(draftId)}`,
          { method: 'DELETE' }
        );
      } catch {
        /* ignore cleanup failure */
      }
      throw err;
    }
  }

  // Shared threaded reply-all send. Defined INSIDE register() (closes over session-scoped graphFetch /
  // auth / whitelist / replyPolicy / attachAndSendDraft). Resolves the set Graph will actually deliver to
  // — (replyTo || from) ∪ toRecipients ∪ ccRecipients, minus the agent's own mailbox — and gates EVERY
  // address against core/email_whitelist. A recipient not on the whitelist is handled per outlook/
  // reply_policy: 'block' returns { blocked } WITHOUT any Graph mutation (block-whole, mirrors
  // outlook_send_mail); 'remove' (default) drops the blocked address, PATCHing the draft to the
  // whitelisted subset so a group thread is never blocked by one bad address (only whitelisted addresses
  // ever receive mail either way). If EVERY recipient is blocked under 'remove', returns { noRecipients }
  // (cannot reply to nobody). createReplyAll (draft) → [PATCH recipients] → attach+send. Returns
  // { recipients, dropped } on success.
  async function createReplyAllDraft(mailbox, messageId, body) {
    const draftRes = await graphFetch(
      `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(messageId)}/createReplyAll`,
      { method: 'POST', body: JSON.stringify({ message: { body: { contentType: 'HTML', content: body } } }) }
    );
    await ensureOk(draftRes);
    return (await draftRes.json())?.id;
  }

  async function sendThreadedReply(mailbox, messageId, body, records) {
    const metaRes = await graphFetch(
      `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(messageId)}` +
        '?$select=from,replyTo,toRecipients,ccRecipients'
    );
    await ensureOk(metaRes);
    const meta = await metaRes.json();
    const origin = (meta?.replyTo && meta.replyTo.length)
      ? meta.replyTo.map((r) => r.emailAddress?.address)
      : [meta?.from?.emailAddress?.address];
    const to = (meta?.toRecipients || []).map((r) => r.emailAddress?.address);
    const cc = (meta?.ccRecipients || []).map((r) => r.emailAddress?.address);
    const selfAddresses = buildSelfAddresses(mailbox, mailboxAliases);
    const notSelf = (a) => a && !selfAddresses.has(a.trim().toLowerCase());
    // Case-insensitive dedupe + self-exclusion so an address isn't gated/reported twice and the agent's
    // own mailbox (which createReplyAll drops server-side) isn't gated.
    const recipients = dedupeCI([...origin, ...to, ...cc].filter(notSelf));
    const blocked = recipients.filter((addr) => !matchesWhitelist(addr, whitelist));

    if (blocked.length === 0) {
      // Fast path (both policies): nothing to drop — let createReplyAll's own recipients stand.
      const draftId = await createReplyAllDraft(mailbox, messageId, body);
      await attachAndSendDraft(mailbox, draftId, records);
      return { recipients };
    }

    if (replyPolicy === 'block') return { blocked };

    // reply_policy=remove: rebuild the whitelisted reply-all buckets — original sender → To, surviving
    // To/Cc → Cc (matches createReplyAll's own convention) — and PATCH them onto the draft. Promote Cc→To
    // if the sender was dropped so the message always has a To recipient; if NOTHING survives, don't send.
    const asRecipients = (addrs) => addrs.map((a) => ({ emailAddress: { address: a } }));
    let toB = dedupeCI(origin.filter(notSelf)).filter((a) => matchesWhitelist(a, whitelist));
    const toKeys = new Set(toB.map((a) => a.toLowerCase()));
    let ccB = dedupeCI([...to, ...cc].filter(notSelf))
      .filter((a) => matchesWhitelist(a, whitelist) && !toKeys.has(a.toLowerCase()));
    if (toB.length === 0) { toB = ccB; ccB = []; }
    if (toB.length === 0) return { noRecipients: true };

    const draftId = await createReplyAllDraft(mailbox, messageId, body);
    await attachAndSendDraft(mailbox, draftId, records, { to: asRecipients(toB), cc: asRecipients(ccB) });
    return { recipients: [...toB, ...ccB], dropped: blocked };
  }

  function notConfigured(toolName) {
    log(toolName, 'ERROR', 'Graph API not configured');
    return {
      content: [{ type: 'text', text: 'Error: Graph API not configured (set outlook_* config and a cert or client secret).' }],
      isError: true,
    };
  }

  // --- outlook_list_thread (opt-in via tools/outlook_list_thread/is_enabled; NOT registered when off) ---
  // Takes NO parameters: the conversation is derived from THIS job's trigger message, so the agent can
  // never choose which conversation it lists. Returns a lightweight, chronological (oldest→newest) index
  // of the authorized messages in that conversation with attachment METADATA only — the agent then uses
  // outlook_get_message / outlook_get_attachment (already extended to the thread) to fetch what it needs.
  if (enabled('outlook_list_thread')) {
    server.tool(
      'outlook_list_thread',
      [
        "List the earlier messages of THIS email task's own Outlook thread (same conversation as the",
        'triggering message). Takes no parameters. Returns a chronological index (oldest→newest) with, per',
        'message: message_id, direction, from, received_at, subject, preview, has_attachments and attachment',
        'metadata (attachment_id, name, content_type, size) — NOT the bodies or file contents. Use it before',
        'asking the sender to re-send an earlier file: fetch the full message with outlook_get_message or a',
        'file with outlook_get_attachment using a message_id from this index. Reading only — you cannot reply',
        'to or mark an earlier message. The result also carries "truncated" (older messages beyond the cap',
        'were omitted) and "incomplete" (a transient Graph error left some attachment metadata unresolved —',
        'a per-message "attachments_incomplete" flag marks which; simply call again to recover it).',
      ].join('\n'),
      {},
      async () => {
        if (!auth.isConfigured()) return notConfigured('outlook_list_thread');
        try {
          const thread = await resolveThreadBinding();
          if (!thread.bound) {
            log('outlook_list_thread', 'BLOCKED', 'thread not available for this task');
            return { content: [{ type: 'text', text: 'Error: thread is not available for this task.' }], isError: true };
          }
          return {
            content: [
              {
                type: 'text',
                text: JSON.stringify(
                  { messages: thread.messages, truncated: thread.truncated, incomplete: Boolean(thread.incomplete) },
                  null,
                  2
                ),
              },
            ],
          };
        } catch (err) {
          log('outlook_list_thread', 'ERROR', `${err.message}`);
          return { content: [{ type: 'text', text: 'Error: thread is not available for this task.' }], isError: true };
        }
      }
    );
  }

  // --- outlook_get_message ---
  if (enabled('outlook_get_message')) {
    server.tool(
      'outlook_get_message',
      [
        'Read a full email message from the Outlook mailbox by message ID.',
        'Returns: subject, from, to, cc, body (text), receivedDateTime, conversationId, hasAttachments,',
        'attachments[] (metadata: attachment_id, name, contentType, size, isInline, type) — use',
        'outlook_get_attachment to download a file.',
      ].join('\n'),
      { message_id: z.string().describe('Graph message ID') },
      async ({ message_id }) => {
        if (!auth.isConfigured()) return notConfigured('outlook_get_message');
        // Current-job READ binding: a headless job may read its own triggering message, plus (when
        // outlook_list_thread is enabled) an already-authorized message of the same conversation. A foreign id
        // (leaked/guessed) is refused with a generic error that leaks nothing about why, before any Graph
        // call — and WITHOUT a Graph GET of that id to discover its conversation.
        const read = await classifyRead(message_id);
        if (!read.allowed) {
          log('outlook_get_message', 'BLOCKED', 'message id not bound to the current job');
          return { content: [{ type: 'text', text: 'Error: message is not available for this task.' }], isError: true };
        }
        const mailbox = auth.getMailboxUserId();
        try {
          const res = await graphFetch(
            `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(message_id)}` +
              '?$select=subject,body,from,toRecipients,ccRecipients,receivedDateTime,conversationId,hasAttachments,internetMessageHeaders'
          );
          await ensureOk(res);
          const msg = await res.json();
          const result = {
            subject: msg.subject,
            from: { name: msg.from?.emailAddress?.name, address: msg.from?.emailAddress?.address },
            to: (msg.toRecipients || []).map((r) => ({ name: r.emailAddress?.name, address: r.emailAddress?.address })),
            cc: (msg.ccRecipients || []).map((r) => ({ name: r.emailAddress?.name, address: r.emailAddress?.address })),
            body: msg.body?.content,
            bodyType: msg.body?.contentType,
            receivedDateTime: msg.receivedDateTime,
            conversationId: msg.conversationId,
            hasAttachments: msg.hasAttachments,
          };
          // Gate stays FIRST: a blocked message lists nothing (no attachment fetch happens). A 'thread'
          // message was already authorized while building the thread binding (including trusted outbound,
          // which surfaceAllowed would wrongly reject), so it is not re-gated here.
          if (read.source !== 'thread' && !surfaceAllowed(result.from.address, msg.internetMessageHeaders)) {
            log('outlook_get_message', 'BLOCKED', `mailbox=${mailbox} sender not allow-listed or DMARC not pass (read restricted)`);
            return {
              content: [{ type: 'text', text: 'Error: message sender is not in allowed_senders or did not pass DMARC; reading is restricted (set outlook/restrict_read_to_allowed_senders=false to allow — security risk).' }],
              isError: true,
            };
          }
          // After the gate passes, fetch attachment METADATA only ($select excludes contentBytes, so no
          // bytes are pulled). Own try/catch: a transient metadata error must NOT hide an already-gated-OK
          // body. @odata.type is a control annotation (not always $select-able) — Graph returns it by
          // default on every polymorphic attachment, so read it from the response.
          if (msg.hasAttachments) {
            try {
              const ar = await graphFetch(
                `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(message_id)}/attachments` +
                  '?$select=id,name,contentType,size,isInline'
              );
              await ensureOk(ar);
              const data = await ar.json();
              result.attachments = (data.value || []).map((a) => ({
                attachment_id: a.id,
                name: a.name,
                contentType: a.contentType,
                size: a.size,
                isInline: a.isInline,
                type: odataAttachmentType(a['@odata.type']),
              }));
            } catch (err) {
              log('outlook_get_message', 'WARN', `mailbox=${mailbox} attachment metadata fetch failed: ${err.message}`);
              result.attachments = [];
            }
          }
          log('outlook_get_message', 'OK', `mailbox=${mailbox} subject="${msg.subject}" from=${result.from.address}`);
          return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
        } catch (err) {
          log('outlook_get_message', 'ERROR', `mailbox=${mailbox} ${err.message}`);
          return { content: [{ type: 'text', text: `Error reading message: ${err.message}` }], isError: true };
        }
      }
    );
  }

  // --- outlook_get_attachment (download ONE attachment to the artifacts dir; opt-in) ---
  if (enabled('outlook_get_attachment')) {
    server.tool(
      'outlook_get_attachment',
      [
        'Download one file attachment from an email to the job artifacts directory.',
        'For the triggering message (or an inbound thread message) it re-applies the SAME read-gate as the',
        'read tools (sender allow-listed + DMARC pass) before any download; a message already authorized by',
        'the thread binding — including a trusted outbound one the agent itself sent from Sent Items — is not',
        're-gated (so you can recover your own earlier attachment). Rejects non-file attachments and anything',
        'over 25 MB, and returns { path, name, contentType, size }. The saved path can then be attached to a',
        'reply or a new email.',
      ].join('\n'),
      {
        message_id: z.string().describe('Graph message ID the attachment belongs to'),
        attachment_id: z.string().describe('Attachment ID from outlook_get_message attachments[]'),
      },
      async ({ message_id, attachment_id }) => {
        if (!auth.isConfigured()) return notConfigured('outlook_get_attachment');
        // Current-job READ binding: attachments may be downloaded from this job's triggering message, plus
        // (when outlook_list_thread is enabled) an already-authorized message of the same conversation. A foreign
        // message id is refused generically (no leak) before any Graph call — and without a Graph GET of
        // that id to discover its conversation.
        const read = await classifyRead(message_id);
        if (!read.allowed) {
          log('outlook_get_attachment', 'BLOCKED', 'message id not bound to the current job');
          return { content: [{ type: 'text', text: 'Error: message is not available for this task.' }], isError: true };
        }
        const mailbox = auth.getMailboxUserId();
        try {
          // 1. Re-apply the read gate BEFORE any download. Same message GET shape the read tools use
          //    (from + internetMessageHeaders). Blocked ⇒ isError, NO $value GET issued. A 'thread' message
          //    was already authorized while building the binding (incl. trusted outbound), so it is not
          //    re-gated — that also avoids blocking the agent's own Sent-Items attachment.
          if (read.source !== 'thread') {
            const gateRes = await graphFetch(
              `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(message_id)}?$select=from,internetMessageHeaders`
            );
            await ensureOk(gateRes);
            const gateMsg = await gateRes.json();
            if (!surfaceAllowed(gateMsg.from?.emailAddress?.address, gateMsg.internetMessageHeaders)) {
              log('outlook_get_attachment', 'BLOCKED', `mailbox=${mailbox} sender not allow-listed or DMARC not pass (read restricted)`);
              return {
                content: [{ type: 'text', text: 'Error: message sender is not in allowed_senders or did not pass DMARC; downloading is restricted (set outlook/restrict_read_to_allowed_senders=false to allow — security risk).' }],
                isError: true,
              };
            }
          }

          // 2. Metadata + type/size guard. Read @odata.type from the RESPONSE (not $select). Fail closed:
          //    only #microsoft.graph.fileAttachment may be downloaded.
          const metaRes = await graphFetch(
            `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(message_id)}/attachments/${encodeURIComponent(attachment_id)}?$select=id,name,contentType,size`
          );
          await ensureOk(metaRes);
          const meta = await metaRes.json();
          if (meta['@odata.type'] !== '#microsoft.graph.fileAttachment') {
            log('outlook_get_attachment', 'ERROR', `mailbox=${mailbox} non-file attachment type=${meta['@odata.type']}`);
            return { content: [{ type: 'text', text: 'Error: only file attachments can be downloaded (item/reference attachments are not supported).' }], isError: true };
          }
          if (typeof meta.size === 'number' && meta.size > MAX_ATTACHMENT_BYTES) {
            log('outlook_get_attachment', 'ERROR', `mailbox=${mailbox} attachment ${meta.size} bytes exceeds 25 MB`);
            return { content: [{ type: 'text', text: `Error: attachment is ${meta.size} bytes; exceeds the 25 MB cap.` }], isError: true };
          }

          // 3. Download raw bytes. Reject early if the response advertises a too-large Content-Length.
          const valRes = await graphFetch(
            `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(message_id)}/attachments/${encodeURIComponent(attachment_id)}/$value`
          );
          await ensureOk(valRes);
          const declaredLen = Number(valRes.headers?.get?.('Content-Length'));
          if (Number.isFinite(declaredLen) && declaredLen > MAX_ATTACHMENT_BYTES) {
            log('outlook_get_attachment', 'ERROR', `mailbox=${mailbox} Content-Length ${declaredLen} exceeds 25 MB`);
            return { content: [{ type: 'text', text: `Error: attachment is ${declaredLen} bytes; exceeds the 25 MB cap.` }], isError: true };
          }
          const bytes = Buffer.from(await valRes.arrayBuffer());

          // 4. Enforce the REAL cap on the actual bytes (metadata can lie), then write symlink-safely.
          if (bytes.length > MAX_ATTACHMENT_BYTES) {
            log('outlook_get_attachment', 'ERROR', `mailbox=${mailbox} downloaded ${bytes.length} bytes exceeds 25 MB`);
            return { content: [{ type: 'text', text: `Error: attachment is ${bytes.length} bytes; exceeds the 25 MB cap.` }], isError: true };
          }
          let safeName = basename(meta.name || 'attachment');
          if (!safeName || safeName === '.' || safeName === '..') safeName = 'attachment';

          // /workspace contents are agent-writable, so the agent could pre-create the artifacts path (or
          // an intermediate component) as a symlink pointing outside /workspace. Create the dir
          // symlink-safely (never following a symlink, never creating outside the real workspace) and use
          // the resolved, contained path it returns; throws → isError, no write.
          let artifactsReal;
          try {
            artifactsReal = await mkdirWithinWorkspace(artifactsDir);
          } catch (err) {
            log('outlook_get_attachment', 'ERROR', `mailbox=${mailbox} artifacts dir escapes /workspace: ${err.message}`);
            return { content: [{ type: 'text', text: 'Error: could not write the attachment (artifacts directory is not inside /workspace).' }], isError: true };
          }
          let outPath = join(artifactsReal, safeName);
          // belt-and-suspenders lexical check (the realpath containment above is the real guard).
          if (!resolve(outPath).startsWith(artifactsReal + sep)) {
            log('outlook_get_attachment', 'ERROR', `mailbox=${mailbox} attachment name escapes artifacts dir`);
            return { content: [{ type: 'text', text: 'Error: attachment name is not allowed.' }], isError: true };
          }

          // No silent overwrite, race-free: exclusive-create (wx); on EEXIST bump a numeric suffix before
          // the extension and retry, so "no overwrite" is enforced by the filesystem, not a check gap.
          let written = false;
          let attempt = 0;
          while (!written) {
            try {
              await writeFile(outPath, bytes, { flag: 'wx' });
              written = true;
            } catch (err) {
              if (err.code !== 'EEXIST') throw err;
              attempt += 1;
              const dot = safeName.lastIndexOf('.');
              const stem = dot > 0 ? safeName.slice(0, dot) : safeName;
              const ext = dot > 0 ? safeName.slice(dot) : '';
              outPath = join(artifactsReal, `${stem}-${attempt}${ext}`);
            }
          }

          log('outlook_get_attachment', 'OK', `mailbox=${mailbox} wrote ${basename(outPath)} (${bytes.length} bytes)`);
          return {
            content: [
              {
                type: 'text',
                text: JSON.stringify(
                  { path: outPath, name: basename(outPath), contentType: meta.contentType, size: bytes.length },
                  null,
                  2
                ),
              },
            ],
          };
        } catch (err) {
          log('outlook_get_attachment', 'ERROR', `mailbox=${mailbox} ${err.message}`);
          return { content: [{ type: 'text', text: `Error downloading attachment: ${err.message}` }], isError: true };
        }
      }
    );
  }

  // --- outlook_reply (reply-all: every delivered recipient must be whitelisted) ---
  if (enabled('outlook_reply')) {
    server.tool(
      'outlook_reply',
      [
        'Reply to an email message. Creates a proper threaded REPLY-ALL (Re: subject, correct headers) —',
        'it goes to the original sender plus everyone on the To/Cc (excluding your own mailbox), keeping',
        'the whole conversation in one thread. Only recipients in the email whitelist (core/email_whitelist)',
        'ever receive it; a non-whitelisted recipient is dropped from the reply by default (reply_policy),',
        'and the result tells you who was omitted. (If reply_policy=block, one non-whitelisted recipient',
        'blocks the whole send instead.) For a targeted 1:1 message to a specific address, use outlook_send_mail instead.',
        'The body must be valid HTML (e.g. <p>, <ul>/<li>, <b>) — it is sent as an HTML message body.',
        'Optional attachments: absolute file paths inside /workspace/ (e.g. files downloaded via',
        'outlook_get_attachment). Max 10 files; each up to 25 MB.',
      ].join('\n'),
      {
        message_id: z.string().describe('Graph message ID to reply to'),
        body: z.string().describe('Reply body as HTML markup (use <p>, <ul>/<li>, <b>; not plain text or markdown)'),
        attachments: z
          .array(z.string())
          .max(10)
          .optional()
          .describe('Absolute file paths inside /workspace/. Max 10 files; each up to 25 MB.'),
      },
      async ({ message_id, body, attachments }) => {
        if (!auth.isConfigured()) return notConfigured('outlook_reply');
        // Bind to the current job's triggering message: a leaked id must not let the agent reply-all into
        // (or fetch recipient metadata from) another conversation. Thread read does NOT widen this — an
        // action always targets the trigger. Runs before ANY Graph call.
        if (!(await jobActionBindingAllows(message_id))) {
          log('outlook_reply', 'BLOCKED', 'message id not bound to the current job');
          return { content: [{ type: 'text', text: 'Error: message is not available for this task.' }], isError: true };
        }
        const mailbox = auth.getMailboxUserId();
        try {
          // With attachments: validate FIRST so bad input returns isError before ANY Graph call.
          let records = [];
          if (attachments?.length) {
            const v = await validateAttachments(attachments);
            if (v.error) {
              log('outlook_reply', 'ERROR', `mailbox=${mailbox} ${v.error}`);
              return { content: [{ type: 'text', text: v.error }], isError: true };
            }
            records = v.records;
          }

          const result = await sendThreadedReply(mailbox, message_id, body, records);
          if (result.blocked) {
            // reply_policy=block: one non-whitelisted recipient blocks the whole send.
            log('outlook_reply', 'BLOCKED', `mailbox=${mailbox} recipient(s)="${result.blocked.join(',')}" not in whitelist`);
            return {
              content: [{ type: 'text', text: `Error: reply recipient "${result.blocked.join(', ')}" is not in the allowed recipients whitelist.` }],
              isError: true,
            };
          }
          if (result.noRecipients) {
            // reply_policy=remove but EVERY recipient was non-whitelisted → nothing left to reply to.
            log('outlook_reply', 'BLOCKED', `mailbox=${mailbox} all reply recipients not in whitelist; nothing sent`);
            return {
              content: [{ type: 'text', text: 'Error: none of the reply recipients are in the allowed recipients whitelist; nothing was sent. Use outlook_send_mail to reach a specific allowed address.' }],
              isError: true,
            };
          }
          const dropped = result.dropped || [];
          log('outlook_reply', 'OK', `mailbox=${mailbox} to=${result.recipients.join(',')}${dropped.length ? ` dropped=${dropped.join(',')}` : ''} attachments=${records.length} message_id=${message_id.slice(0, 20)}...`);
          const attachNote = records.length ? ` (${records.length} attachment${records.length === 1 ? '' : 's'})` : '';
          const dropNote = dropped.length
            ? ` ${dropped.length} recipient${dropped.length === 1 ? '' : 's'} omitted (not in the allowed recipients whitelist): ${dropped.join(', ')}.`
            : '';
          return {
            content: [{ type: 'text', text: `Reply sent successfully${attachNote}.${dropNote}` }],
          };
        } catch (err) {
          log('outlook_reply', 'ERROR', `mailbox=${mailbox} ${err.message}`);
          return { content: [{ type: 'text', text: `Error sending reply: ${err.message}` }], isError: true };
        }
      }
    );
  }

  // --- outlook_send_mail (recipient-gated) ---
  if (enabled('outlook_send_mail')) {
    server.tool(
      'outlook_send_mail',
      [
        "Send a new email from the agent's Outlook mailbox. PREFERRED over the SMTP `email_send` when",
        "available — it sends from the agent's real mailbox via Microsoft Graph (correct sender identity,",
        'DMARC/SPF alignment) and supports attachments.',
        'All recipients (to, cc, bcc) must be in the email whitelist (core/email_whitelist).',
        'The body must be valid HTML (e.g. <p>, <ul>/<li>, <b>) — it is sent as an HTML message body.',
        'Optional attachments: absolute file paths inside /workspace/. Max 10 files; each up to 25 MB.',
      ].join('\n'),
      {
        to: z.array(z.string().email()).describe('Recipient email addresses'),
        subject: z.string().describe('Email subject'),
        body: z.string().describe('Email body as HTML markup (use <p>, <ul>/<li>, <b>; not plain text or markdown)'),
        cc: z.array(z.string().email()).optional().describe('CC recipient email addresses'),
        bcc: z.array(z.string().email()).optional().describe('Blind carbon-copy recipients. All must be in the whitelist.'),
        attachments: z
          .array(z.string())
          .max(10)
          .optional()
          .describe('Absolute file paths inside /workspace/. Max 10 files; each up to 25 MB.'),
      },
      async ({ to, subject, body, cc, bcc, attachments }) => {
        if (!auth.isConfigured()) return notConfigured('outlook_send_mail');
        // Recipient gate is the file-exfiltration choke point and runs FIRST (covers To+Cc+Bcc; Bcc is a
        // real, invisible recipient). Blocked ⇒ isError, no Graph call.
        const allRecipients = [...to, ...(cc || []), ...(bcc || [])];
        const blocked = allRecipients.filter((addr) => !matchesWhitelist(addr, whitelist));
        if (blocked.length > 0) {
          log('outlook_send_mail', 'BLOCKED', `blocked=${blocked.join(',')}`);
          return { content: [{ type: 'text', text: `Error: Recipients not in whitelist: ${blocked.join(', ')}` }], isError: true };
        }
        const mailbox = auth.getMailboxUserId();
        const toRecipients = to.map((addr) => ({ emailAddress: { address: addr } }));
        const ccRecipients = (cc || []).map((addr) => ({ emailAddress: { address: addr } }));
        const bccRecipients = (bcc || []).map((addr) => ({ emailAddress: { address: addr } }));
        try {
          // Attachments must ride a draft (never inline /sendMail attachments — Graph caps the whole
          // inline payload at 4 MB). With attachments: validate FIRST (isError before ANY Graph call);
          // the shared helper attaches → sends → DELETEs the draft on failure. No attachments → the
          // single-shot /sendMail below.
          if (attachments?.length) {
            const v = await validateAttachments(attachments);
            if (v.error) {
              log('outlook_send_mail', 'ERROR', `mailbox=${mailbox} ${v.error}`);
              return { content: [{ type: 'text', text: v.error }], isError: true };
            }
            const records = v.records;
            // POST /messages creates a draft in Drafts; the shared helper finishes it.
            const draftRes = await graphFetch(`/users/${encodeURIComponent(mailbox)}/messages`, {
              method: 'POST',
              body: JSON.stringify({
                subject,
                body: { contentType: 'HTML', content: body },
                toRecipients,
                ...(ccRecipients.length > 0 && { ccRecipients }),
                ...(bccRecipients.length > 0 && { bccRecipients }),
              }),
            });
            await ensureOk(draftRes);
            const draftId = (await draftRes.json())?.id;
            await attachAndSendDraft(mailbox, draftId, records);
            log('outlook_send_mail', 'OK', `mailbox=${mailbox} to=${to.join(',')} attachments=${records.length} subject="${subject}"`);
            return {
              content: [{
                type: 'text',
                text: records.length
                  ? `Email sent successfully (${records.length} attachment${records.length === 1 ? '' : 's'}).`
                  : 'Email sent successfully.',
              }],
            };
          }

          // No attachments: the single-shot /sendMail POST (also carrying bccRecipients when present,
          // mirroring ccRecipients).
          const res = await graphFetch(`/users/${encodeURIComponent(mailbox)}/sendMail`, {
            method: 'POST',
            body: JSON.stringify({
              message: {
                subject,
                body: { contentType: 'HTML', content: body },
                toRecipients,
                ...(ccRecipients.length > 0 && { ccRecipients }),
                ...(bccRecipients.length > 0 && { bccRecipients }),
              },
            }),
          });
          await ensureOk(res);
          log('outlook_send_mail', 'OK', `mailbox=${mailbox} to=${to.join(',')} subject="${subject}"`);
          return { content: [{ type: 'text', text: 'Email sent successfully.' }] };
        } catch (err) {
          log('outlook_send_mail', 'ERROR', `mailbox=${mailbox} ${err.message}`);
          return { content: [{ type: 'text', text: `Error sending email: ${err.message}` }], isError: true };
        }
      }
    );
  }

  // --- outlook_mark_processed ---
  if (enabled('outlook_mark_processed')) {
    server.tool(
      'outlook_mark_processed',
      ['Mark an email message as read (processed).', 'Use after finishing an email task.'].join('\n'),
      { message_id: z.string().describe('Graph message ID to mark as read') },
      async ({ message_id }) => {
        if (!auth.isConfigured()) return notConfigured('outlook_mark_processed');
        // Bind to the current job's triggering message: a leaked id must not let the agent flip isRead on
        // another conversation's mail. Thread read does NOT widen this — an action always targets the
        // trigger. Runs before any Graph call.
        if (!(await jobActionBindingAllows(message_id))) {
          log('outlook_mark_processed', 'BLOCKED', 'message id not bound to the current job');
          return { content: [{ type: 'text', text: 'Error: message is not available for this task.' }], isError: true };
        }
        const mailbox = auth.getMailboxUserId();
        try {
          const res = await graphFetch(
            `/users/${encodeURIComponent(mailbox)}/messages/${encodeURIComponent(message_id)}`,
            { method: 'PATCH', body: JSON.stringify({ isRead: true }) }
          );
          await ensureOk(res);
          log('outlook_mark_processed', 'OK', `mailbox=${mailbox} message_id=${message_id.slice(0, 20)}...`);
          return { content: [{ type: 'text', text: 'Message marked as read.' }] };
        } catch (err) {
          log('outlook_mark_processed', 'ERROR', `mailbox=${mailbox} ${err.message}`);
          return { content: [{ type: 'text', text: `Error marking message as read: ${err.message}` }], isError: true };
        }
      }
    );
  }
}
