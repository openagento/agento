// The share URL (PRD E6 §9.1): `https://<token>.<AGENTO_SHARE_HOST>[:port]/`. The host is
// the same env value the proxy renders into its share site, checked by the same rule
// (SEC-5; fixture tests/fixtures/share_host_v1.json). An empty or invalid value means
// shares are off here, where the proxy refuses to start on an invalid one.

const LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
export const SHARE_TOKEN_RE = /^[0-9a-f]{32}$/;

export function validShareHost(host) {
  if (typeof host !== 'string' || host.length > 253) return false;
  const labels = host.split('.');
  return labels.length >= 2 && labels.every((l) => LABEL.test(l));
}

let warned = false;

/** The share URL for `token`, or null when shares are not configured. */
export function shareUrl(token, env = process.env, log = null) {
  const host = env.AGENTO_SHARE_HOST ?? '';
  const port = String(env.AGENTO_PROXY_PORT ?? '443');
  const portOk = /^[0-9]{1,5}$/.test(port) && Number(port) >= 1 && Number(port) <= 65535;
  if (!host || !validShareHost(host) || !portOk) {
    if (host && !warned) {
      warned = true;
      log?.('versioned_artifacts', 'WARNING', 'AGENTO_SHARE_HOST or AGENTO_PROXY_PORT is invalid: shares are off');
    }
    return null;
  }
  if (!SHARE_TOKEN_RE.test(token ?? '')) return null;
  return `https://${token}.${host}${Number(port) === 443 ? '' : `:${Number(port)}`}/`;
}

/** The panel origin, `https://<AGENTO_PANEL_HOST>[:port]`, the same value web checks a
 *  request's Origin against. A page on the apps origin needs it to reach the panel bridge,
 *  and an agent has no other trusted source for it. Null when it is not configured. */
export function panelOrigin(env = process.env) {
  const host = env.AGENTO_PANEL_HOST ?? '';
  const port = String(env.AGENTO_PROXY_PORT ?? '443');
  if (!validShareHost(host) || !/^[0-9]{1,5}$/.test(port) || Number(port) < 1 || Number(port) > 65535) return null;
  return `https://${host}${Number(port) === 443 ? '' : `:${Number(port)}`}`;
}
