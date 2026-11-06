import { registerTools, loadScopedDbOverridesStrict, loadWorkspaceOverridesStrict } from './config-loader.js';
import { createScopedLogger, createPhasedLogger } from './log.js';
import { buildArtifactsDir, FALLBACK_ARTIFACTS_DIR, loadDownloadsDir } from './artifacts-dir.js';

// Registers the tools a capability's scope enables on `server` and returns the dispatcher's
// registry. Shared by MCP sessions and invoke, so both run the same handlers with the same
// offload path.
export async function buildRegistration(server, authContext, { context, dbQuery, runId = null }) {
  const { agent_view_id: agentViewId, job_id: jobId } = authContext;
  let artifactsDir = FALLBACK_ARTIFACTS_DIR;
  // jobId (from the capability row, null for interactive runs / tool-list) flows to every tool's
  // register() via registerTools -> enrichedContext; schedule_followup uses it to inherit the
  // current job's channel/reference/scope.
  // The job's thread dir for downloads two turns of one thread share (Jira attachments),
  // null without a job: every caller keeps artifactsDir then.
  const downloadsDir = await loadDownloadsDir(dbQuery, jobId);
  let sessionContext = { ...context, artifactsDir, downloadsDir, jobId };
  let preloadedOverrides;
  let agentViewMeta = null;
  if (agentViewId) {
    ({ overrides: preloadedOverrides, agentViewMeta } = await loadScopedDbOverridesStrict(agentViewId));
    if (agentViewMeta) {
      artifactsDir = buildArtifactsDir(agentViewMeta, jobId, runId);
      sessionContext = { ...sessionContext, artifactsDir };
    }
  } else {
    preloadedOverrides = await loadWorkspaceOverridesStrict(authContext.workspace_id);
  }
  // The MCP tool-invocation logger for this session. Every line names the verified caller from
  // authContext, with the agent_view part when the view is known.
  const invocationLog = createScopedLogger(agentViewMeta, authContext);

  // Registration-time diagnostics that modules emit from register() (e.g. browser SESSION/INIT)
  // are lifecycle noise and must stay out of toolbox_mcp.log. The phased logger routes them to
  // toolbox_rest.log, then flips to invocationLog once registration completes — before any tool
  // handler can run — so only real invocations reach toolbox_mcp.log.
  const sessionLog = createPhasedLogger(invocationLog);
  sessionContext = { ...sessionContext, log: sessionLog };
  const registration = await registerTools(server, sessionContext, agentViewId, preloadedOverrides);
  sessionLog.toInvocationPhase();
  return registration;
}
