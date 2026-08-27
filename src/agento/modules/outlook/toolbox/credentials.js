// The Graph credential probe. It lives here because this is the only place the
// Graph credentials exist (see modules/outlook/src/config.py — the Python config
// dataclass deliberately omits them). It acquires a token and nothing else: no
// mail is read, sent or enumerated.
//
// Two exports over one probe:
//   configTests  — the per-field Test action (four states, framework vocabulary)
//   healthcheck  — the /health fan-out entry (ok/fail/skip, healthcheck vocabulary)
import { createGraphAuth } from './graph-auth.js';

const TOOL = 'outlook';

const PATHS = [
  'outlook/outlook_tenant_id',
  'outlook/outlook_client_id',
  'outlook/outlook_mailbox_user_id',
  'outlook/outlook_client_secret',
  'outlook/outlook_cert_pem',
  'outlook/outlook_cert_password',
];

const NOT_CONFIGURED_DETAIL =
  'needs a tenant id, a client id, a mailbox user id, and either a client secret '
  + 'or a certificate';

// Whatever the provider says, only a coarse reason crosses this boundary.
// graph-auth already reduces a credential error to its code, but a config value
// could still appear in some other thrown message, so the code is extracted and
// the rest is dropped. (runConfigTest redacts on top of this — belt and braces,
// because /health does not go through that redaction for module-shaped errors.)
function reasonOf(err) {
  const text = String((err && err.message) || err || '');
  const found = text.match(/\(([a-z0-9_]{3,40})\)\s*$/i);
  return found ? found[1] : '';
}

// Only the identity provider can reject a credential. A DNS miss, a refused
// socket or a timeout means nothing was rejected — that is `error`, matching the
// smtp and http probes. MSAL surfaces transport trouble either as a node error
// code or as one of its own `network`/`timeout` labels.
const TRANSPORT = /ENOTFOUND|ECONNREFUSED|ECONNRESET|EAI_AGAIN|ETIMEDOUT|ABORT_ERR|network_error|request_timeout|endpoints_resolution_error/i;

function statusOf(err, reason) {
  const text = `${(err && err.code) || ''} ${(err && err.message) || ''} ${reason}`;
  return TRANSPORT.test(text) ? 'error' : 'fail';
}

/** The shared probe. Returns the framework's four-state vocabulary. */
async function probeGraph(config, deps = {}, signal = undefined) {
  const factory = deps.graphAuthFactory || createGraphAuth;
  const auth = factory(config || {});

  if (!auth.isConfigured()) {
    return { status: 'not_configured', code: 'NOT_SET', detail: NOT_CONFIGURED_DETAIL };
  }

  const started = Date.now();
  try {
    await auth.getToken(signal ? { abortSignal: signal } : {});
  } catch (err) {
    const reason = reasonOf(err);
    return {
      status: statusOf(err, reason),
      // A shaped code always: the framework drops anything that is not
      // ^[A-Z0-9_]{1,40}$, and an unshaped code would print as UNSPECIFIED.
      code: reason ? reason.toUpperCase().slice(0, 40) : 'TOKEN_FAILED',
      detail: reason
        ? `token acquisition failed (${reason})`
        : 'token acquisition failed',
      ms: Date.now() - started,
    };
  }
  return { status: 'ok', code: 'OK', detail: 'token acquired', ms: Date.now() - started };
}

export const configTests = [
  {
    name: 'graph_credentials',
    paths: PATHS,
    // `config` arrives keyed by FIELD name — the shape createGraphAuth already
    // takes, so there is no adapter here on purpose.
    run: async ({ config, deps, signal }) => probeGraph(config, deps, signal),
  },
];

const HEALTH_STATUS = { ok: 'ok', fail: 'fail', not_configured: 'skip' };

export async function healthcheck({ moduleConfigs, signal } = {}, deps = {}) {
  // `runHealthchecks` already aborts its controller on the healthcheck timeout;
  // passing the signal on is what makes the abort reach the token request, so a
  // slow /health does not leave a login running behind it.
  const cfg = (moduleConfigs && moduleConfigs.outlook) || {};
  const r = await probeGraph(cfg, deps, signal);
  const entry = { tool: TOOL, status: HEALTH_STATUS[r.status] || 'fail' };
  if (r.status !== 'ok') entry.error = r.detail;
  if (r.ms !== undefined) entry.ms = r.ms;
  return [entry];
}
