import { z } from 'zod';
import { stat } from 'node:fs/promises';
import { basename } from 'node:path';
import { createTransporter } from './email-transport.js';

const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;

// `matchesWhitelist` is the shared toolbox matcher — one implementation for the outbound gate
// here and the inbound gate in outlook. It arrives through the registration context because a
// module cannot import framework code by path (see config-loader.js TOOLBOX_HELPERS).
function findRejectedRecipient(matchesWhitelist, whitelist, ...groups) {
  for (const { field, list } of groups) {
    if (!list) continue;
    for (const addr of list) {
      if (!matchesWhitelist(addr, whitelist)) return { field, addr };
    }
  }
  return null;
}

async function validateAttachments(paths) {
  for (const p of paths) {
    if (!p.startsWith('/workspace/')) {
      return `Error: attachment path "${p}" must be inside /workspace/`;
    }
  }
  for (const p of paths) {
    let info;
    try {
      info = await stat(p);
    } catch (err) {
      return `Error: attachment "${p}" not found (${err.code || err.message})`;
    }
    if (!info.isFile()) {
      return `Error: attachment "${p}" is not a regular file`;
    }
    if (info.size > MAX_ATTACHMENT_BYTES) {
      return `Error: attachment "${p}" is ${info.size} bytes; exceeds 25 MB cap`;
    }
  }
  return null;
}

export async function healthcheck({ moduleConfigs, sanitizeHealthError }) {
  const cfg = moduleConfigs?.core || {};
  const smtpConfig = {
    host: cfg.smtp_host || null,
    port: parseInt(cfg.smtp_port || '587'),
    user: cfg.smtp_user || null,
    pass: cfg.smtp_pass || null,
  };

  if (!smtpConfig.host || !smtpConfig.user) {
    return [{ tool: 'email_send', status: 'skip', error: 'not configured' }];
  }

  const start = Date.now();
  try {
    const transporter = await createTransporter({
      host: smtpConfig.host,
      port: smtpConfig.port,
      secure: smtpConfig.port === 465,
      auth: { user: smtpConfig.user, pass: smtpConfig.pass },
    });
    await transporter.verify();
    return [{ tool: 'email_send', status: 'ok', ms: Date.now() - start }];
  } catch (err) {
    return [{ tool: 'email_send', status: 'fail', ms: Date.now() - start, error: sanitizeHealthError(err) }];
  }
}

export function register(server, { log, moduleConfigs, isToolEnabled, matchesWhitelist }) {
  if (isToolEnabled && !isToolEnabled('email_send')) return;
  const cfg = moduleConfigs?.core || {};
  const smtpConfig = {
    host: cfg.smtp_host || null,
    port: parseInt(cfg.smtp_port || '587'),
    user: cfg.smtp_user || null,
    pass: cfg.smtp_pass || null,
    from: cfg.smtp_from || null,
  };

  const whitelist = (cfg.email_whitelist || '')
    .split(',')
    .map(p => p.trim().toLowerCase())
    .filter(Boolean);

  server.tool(
    'email_send',
    [
      'Send a single SMTP email. Supports multiple To/Cc/Bcc recipients and file attachments.',
      "If a channel-native mailbox sender is available for this agent, prefer it (it sends from the agent's real mailbox with correct identity and DMARC alignment); use this SMTP sender when no such sender is enabled.",
      'The body is HTML — wrap any plain text in <pre> or use tags like <p>, <br>, <ul>, <a href> for formatting.',
      'All recipient addresses (to, cc, bcc) must match the whitelist.',
      'Attachment paths must be absolute and inside /workspace/ (typically files in artifactsDir). Max 10 files, 25 MB each.',
      'Example:',
      '  to: ["user@example.com"], cc: ["lead@example.com"],',
      '  subject: "Diagnostic report",',
      '  body_html: "<p>Analysis attached. See <b>chart.png</b> for trend.</p>",',
      '  attachments: ["/workspace/artifacts/ws/av/123/report.pdf"]',
    ].join('\n'),
    {
      user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
      to: z.array(z.string().email()).min(1).describe('Recipient email addresses (To). All must be in the whitelist.'),
      cc: z.array(z.string().email()).optional().describe('Carbon-copy recipients. All must be in the whitelist.'),
      bcc: z.array(z.string().email()).optional().describe('Blind carbon-copy recipients. All must be in the whitelist.'),
      attachments: z.array(z.string()).max(10).optional().describe('Absolute file paths inside /workspace/. Max 10 files; each up to 25 MB.'),
      subject: z.string().describe('Email subject'),
      body_html: z.string().describe('Email body as HTML. Use tags like <p>, <br>, <ul>, <a href>, <b>; wrap plain text in <pre>.'),
    },
    async ({ user, to, cc, bcc, attachments, subject, body_html }) => {
      const rejected = findRejectedRecipient(
        // FAIL-CLOSED: with no matcher wired, no recipient is on the allow list.
        matchesWhitelist || (() => false),
        whitelist,
        { field: 'to', list: to },
        { field: 'cc', list: cc },
        { field: 'bcc', list: bcc },
      );
      if (rejected) {
        log('email_send', 'BLOCKED', `user=${user} ${rejected.field}="${rejected.addr}" - not in whitelist`);
        return {
          content: [{ type: 'text', text: `Error: Recipient "${rejected.addr}" (${rejected.field}) is not in the allowed recipients whitelist.` }],
          isError: true,
        };
      }

      if (!smtpConfig.host) {
        log('email_send', 'ERROR', `user=${user} - SMTP not configured`);
        return {
          content: [{ type: 'text', text: 'Error: SMTP not configured. Set CONFIG__CORE__SMTP_HOST (or bin/agento config:set core/smtp_host).' }],
          isError: true,
        };
      }

      let attachmentsList;
      if (attachments && attachments.length > 0) {
        const err = await validateAttachments(attachments);
        if (err) {
          log('email_send', 'ERROR', `user=${user} ${err}`);
          return { content: [{ type: 'text', text: err }], isError: true };
        }
        attachmentsList = attachments.map(p => ({ filename: basename(p), path: p }));
      }

      try {
        const transporter = await createTransporter({
          host: smtpConfig.host,
          port: smtpConfig.port,
          secure: smtpConfig.port === 465,
          auth: { user: smtpConfig.user, pass: smtpConfig.pass },
        });

        const info = await transporter.sendMail({
          from: smtpConfig.from || smtpConfig.user,
          replyTo: user,
          to: to.join(', '),
          cc: cc?.length ? cc.join(', ') : undefined,
          bcc: bcc?.length ? bcc.join(', ') : undefined,
          subject,
          html: body_html,
          attachments: attachmentsList,
        });

        const counts = `to=${to.length} cc=${cc?.length || 0} bcc=${bcc?.length || 0} attachments=${attachmentsList?.length || 0}`;
        log('email_send', 'OK', `user=${user} ${counts} subject="${subject}" msgId=${info.messageId}`);
        const summary = attachmentsList?.length
          ? `Email sent (${to.length} to, ${cc?.length || 0} cc, ${bcc?.length || 0} bcc, ${attachmentsList.length} attachment${attachmentsList.length === 1 ? '' : 's'}). Message ID: ${info.messageId}`
          : `Email sent (${to.length} to, ${cc?.length || 0} cc, ${bcc?.length || 0} bcc). Message ID: ${info.messageId}`;
        return {
          content: [{ type: 'text', text: summary }],
        };
      } catch (err) {
        log('email_send', 'ERROR', `user=${user} ${err.message}`);
        return {
          content: [{ type: 'text', text: `Email error: ${err.message}` }],
          isError: true,
        };
      }
    }
  );
}
