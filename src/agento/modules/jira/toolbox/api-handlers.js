/**
 * Jira REST API handlers — extracted for testability.
 *
 * Each factory returns an Express handler. Pulled out of api.js so tests
 * can drive the handlers without importing express (api.js's express import
 * cannot be resolved from /src/agento/modules/jira/toolbox/ during vitest).
 *
 * @param {Function} configResolver - async (agentViewId?) => { host, user, token }
 * @param {Function} log - logging function (tool, status, details)
 */

function missingCredFields(user, token, host) {
  return [!user && 'jira_user', !token && 'jira_token', !host && 'jira_host']
    .filter(Boolean)
    .join(',');
}

export function createSearchHandler(configResolver, log) {
  return async (req, res) => {
    const { jql, fields = [], maxResults = 50 } = req.body;
    const agent_view_id = req.capability.agentViewId;

    if (!jql) {
      log('api/jira/search', 'ERROR', `agent_view_id=${agent_view_id ?? '?'} jql missing`);
      return res.status(400).json({ error: 'jql is required' });
    }

    const { user, token, host } = await configResolver(agent_view_id);

    if (!user || !token || !host) {
      log('api/jira/search', 'ERROR',
        `agent_view_id=${agent_view_id ?? '?'} missing=${missingCredFields(user, token, host)}`);
      return res.status(500).json({ error: 'Jira API not configured (jira_host/jira_user/jira_token)' });
    }

    const auth = Buffer.from(`${user}:${token}`).toString('base64');

    try {
      const response = await fetch(`${host}/rest/api/3/search/jql`, {
        method: 'POST',
        headers: {
          'Authorization': `Basic ${auth}`,
          'Accept': 'application/json',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ jql, fields, maxResults }),
      });

      if (!response.ok) {
        const text = await response.text();
        log('api/jira/search', 'ERROR', `HTTP ${response.status}: ${text}`);
        return res.status(response.status).json({ error: text });
      }

      const data = await response.json();
      const count = (data.issues || []).length;
      log('api/jira/search', 'OK', `jql="${jql}" -> ${count} results`);
      return res.json(data);
    } catch (err) {
      log('api/jira/search', 'ERROR', err.message);
      return res.status(500).json({ error: err.message });
    }
  };
}

export function createCommentsHandler(configResolver, log) {
  return async (req, res) => {
    const { issue_key } = req.body;
    const agent_view_id = req.capability.agentViewId;

    if (!issue_key) {
      log('api/jira/issue/comments', 'ERROR', `agent_view_id=${agent_view_id ?? '?'} issue_key missing`);
      return res.status(400).json({ error: 'issue_key is required' });
    }

    const { user, token, host } = await configResolver(agent_view_id);

    if (!user || !token || !host) {
      log('api/jira/issue/comments', 'ERROR',
        `agent_view_id=${agent_view_id ?? '?'} missing=${missingCredFields(user, token, host)}`);
      return res.status(500).json({ error: 'Jira API not configured (jira_host/jira_user/jira_token)' });
    }

    const auth = Buffer.from(`${user}:${token}`).toString('base64');

    try {
      const response = await fetch(
        `${host}/rest/api/2/issue/${encodeURIComponent(issue_key)}/comment?maxResults=100`,
        {
          headers: {
            'Authorization': `Basic ${auth}`,
            'Accept': 'application/json',
          },
        },
      );

      if (!response.ok) {
        const text = await response.text();
        log('api/jira/issue/comments', 'ERROR', `HTTP ${response.status}: ${text}`);
        return res.status(response.status).json({ error: text });
      }

      const data = await response.json();
      const comments = (data.comments || []).map((c) => ({
        id: c.id,
        author: {
          displayName: c.author?.displayName,
          emailAddress: c.author?.emailAddress,
          accountId: c.author?.accountId,
        },
        body: c.body,
        created: c.created,
      }));
      log('api/jira/issue/comments', 'OK', `issue=${issue_key} -> ${comments.length} comments`);
      return res.json({ comments });
    } catch (err) {
      log('api/jira/issue/comments', 'ERROR', err.message);
      return res.status(500).json({ error: err.message });
    }
  };
}
