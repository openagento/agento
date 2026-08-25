import express from 'express';

import { createSearchHandler, createCommentsHandler } from './api-handlers.js';
import { createJiraProxyHandler } from './jira-proxy.js';

export function register(server, { app, log, loadModuleConfigs, loadScopedDbOverridesStrict }) {
  if (!app || !loadModuleConfigs) return;

  async function getJiraConfig(agentViewId = null) {
    let overrides = null;
    if (agentViewId && loadScopedDbOverridesStrict) {
      ({ overrides } = await loadScopedDbOverridesStrict(agentViewId));
    }
    const configs = await loadModuleConfigs(overrides);
    const cfg = configs?.jira || {};
    return {
      host: cfg.jira_host || null,
      user: cfg.jira_user || null,
      token: cfg.jira_token || null,
    };
  }

  // REST API for internal services (cron sync)
  app.post('/api/jira/search', express.json(), createSearchHandler(getJiraConfig, log));

  // REST API for internal services (mention publisher)
  app.post('/api/jira/issue/comments', express.json(), createCommentsHandler(getJiraConfig, log));

  // Generic Jira API proxy (for onboarding and admin operations)
  app.post('/api/jira/request', express.json(), createJiraProxyHandler(getJiraConfig, log));
}
