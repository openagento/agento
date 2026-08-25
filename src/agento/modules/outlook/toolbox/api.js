import express from 'express';
import { createDeltaHandler, deriveFleetMailboxes, deriveMailboxAllowedSenders } from './api-handlers.js';

export function register(server, { app, log, loadModuleConfigs, loadScopedDbOverridesStrict, listActiveAgentViewIds, matchesWhitelist, errorCategory }) {
  // The toolbox calls every module's register() TWICE: once at startup via registerModuleRestApis()
  // (context has `app` + `loadModuleConfigs`) and again on EVERY MCP session via registerTools()
  // (context has NO `loadModuleConfigs`; see config-loader.js). Without this guard the route would be
  // re-registered on every session (Express stacks duplicate handlers → leak). Register REST routes
  // ONLY at startup.
  if (!app || !loadModuleConfigs) return;

  // Resolve the fully-fallen-back outlook config for one agent_view (agent_view -> workspace -> global).
  // The strict resolver THROWS for a view it cannot resolve; server.js maps that to 403/503, so a
  // request never widens to global scope by accident.
  async function resolveOutlookConfig(agentViewId = null) {
    let overrides = null;
    if (agentViewId != null && loadScopedDbOverridesStrict) {
      ({ overrides } = await loadScopedDbOverridesStrict(agentViewId));
    }
    const configs = await loadModuleConfigs(overrides);
    return configs?.outlook || {};
  }

  // Returns { cfg, fleetMailboxes }. `fleetMailboxes` is auto-derived from ALL active agent_views
  // (union of each outlook-enabled view's resolved mailbox) — the hand-maintained agent_mailboxes
  // list is gone.
  async function getOutlookConfig(agentViewId = null) {
    const cfg = await resolveOutlookConfig(agentViewId);
    const fleetMailboxes = await deriveFleetMailboxes(
      {
        listActiveAgentViewIds,
        resolveOutlookConfig,
        excludeMailbox: cfg.outlook_mailbox_user_id, // the polled mailbox → keep only OTHER fleet agents
      },
      log
    );
    return { cfg, fleetMailboxes };
  }
  // The union is per MAILBOX, so it is computed after the handler resolves the mailbox from auth.
  const allowedSendersFor = (mailbox) =>
    deriveMailboxAllowedSenders({ listActiveAgentViewIds, resolveOutlookConfig, mailbox, errorCategory }, log);

  app.post(
    '/api/outlook/delta',
    express.json(),
    createDeltaHandler(getOutlookConfig, log, undefined, allowedSendersFor, matchesWhitelist),
  );
}
