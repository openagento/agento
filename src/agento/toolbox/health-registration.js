import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { registerTools, loadScopedDbOverrides, loadScopedDbOverridesStrict } from './config-loader.js';

// `strict` is for capability-authenticated callers: the lenient resolver answers with GLOBAL
// overrides when a view is missing or the lookup throws, which would widen the scope the
// capability names. The unscoped liveness probe keeps the lenient path.
export async function createHealthRegistration(agentViewId, context, { strict = false } = {}) {
  const server = new McpServer({ name: 'toolbox-health', version: '1.0.0' });
  let overrides = null;

  if (agentViewId) {
    // Scoped overrides drive the correct tool list; the logger stays the caller's
    // (REST/lifecycle) log — /health never invokes tools, so nothing here belongs in
    // toolbox_mcp.log.
    ({ overrides } = strict
      ? await loadScopedDbOverridesStrict(agentViewId)
      : await loadScopedDbOverrides(agentViewId));
  }

  const result = await registerTools(server, context, agentViewId, overrides);
  return {
    tools: result.toolNames,
    healthchecks: result.healthchecks,
    obscureValues: result.obscureValues || [],
  };
}
