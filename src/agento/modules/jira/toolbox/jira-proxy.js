/**
 * Jira API proxy handler — extracted for testability.
 *
 * @param {Function} configResolver - async (agentViewId?) => { host, user, token }
 * @param {Function} log - logging function
 * @returns {Function} Express route handler for POST /api/jira/request
 */
export function createJiraProxyHandler(configResolver, log) {
  return async (req, res) => {
    const config = await configResolver(req.body.agent_view_id || null);

    const { method, path } = req.body;
    const body = req.body.body || null;

    if (!method || !path) {
      log('api/jira/request', 'ERROR', `agent_view_id=${req.body.agent_view_id ?? '?'} method/path missing`);
      return res.status(400).json({ error: 'method and path are required' });
    }

    const ALLOWED_METHODS = ['GET', 'POST', 'PUT', 'DELETE'];
    if (!ALLOWED_METHODS.includes(method.toUpperCase())) {
      log('api/jira/request', 'ERROR', `agent_view_id=${req.body.agent_view_id ?? '?'} invalid method rejected`);
      return res.status(400).json({ error: `Invalid method: ${method}` });
    }

    if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//')) {
      // A fixed reason: the 400 below tells the caller what was wrong, so the record
      // never has to carry the caller's own string into the operator log.
      log('api/jira/request', 'ERROR', `agent_view_id=${req.body.agent_view_id ?? '?'} path rejected: not host-relative`);
      return res.status(400).json({ error: 'path must be a host-relative path' });
    }

    if (req.body.jira_host) {
      log('api/jira/request', 'WARN', 'jira_host in request ignored — the proxy only calls the configured host');
    }

    // Allow per-request credential overrides (for onboarding/admin without agent_view context).
    // The host is NOT overridable: it decides where the Authorization header is sent.
    const host = config.host;
    const user = req.body.auth_user || config.user;
    const token = req.body.auth_token || config.token;

    if (!user || !token || !host) {
      const missing = [!user && 'jira_user', !token && 'jira_token', !host && 'jira_host'].filter(Boolean).join(',');
      log('api/jira/request', 'ERROR', `agent_view_id=${req.body.agent_view_id ?? '?'} missing=${missing}`);
      return res.status(500).json({ error: 'Jira API not configured (jira_host/jira_user/jira_token)' });
    }

    const auth = Buffer.from(`${user}:${token}`).toString('base64');
    const upperMethod = method.toUpperCase();
    const fetchOptions = {
      method: upperMethod,
      headers: {
        'Authorization': `Basic ${auth}`,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      },
    };
    if (body && upperMethod !== 'GET') {
      fetchOptions.body = JSON.stringify(body);
    }

    try {
      const response = await fetch(`${host}${path}`, fetchOptions);
      const text = await response.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }

      if (!response.ok) {
        log('api/jira/request', 'ERROR', `${method} ${path} -> HTTP ${response.status}`);
        return res.status(200).json({ ok: false, status: response.status, data });
      }

      log('api/jira/request', 'OK', `${method} ${path} -> ${response.status}`);
      res.json({ ok: true, status: response.status, data });
    } catch (err) {
      log('api/jira/request', 'ERROR', err.message);
      return res.status(500).json({ error: err.message });
    }
  };
}
