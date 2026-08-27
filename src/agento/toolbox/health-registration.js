import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { registerTools, loadScopedDbOverrides } from './config-loader.js';

export async function createHealthRegistration(agentViewId, context) {
  const server = new McpServer({ name: 'toolbox-health', version: '1.0.0' });
  let overrides = null;

  if (agentViewId) {
    // Scoped overrides drive the correct tool list; the logger stays the caller's
    // (REST/lifecycle) log — /health never invokes tools, so nothing here belongs in
    // toolbox_mcp.log.
    ({ overrides } = await loadScopedDbOverrides(agentViewId));
  }

  const result = await registerTools(server, context, agentViewId, overrides);
  return {
    tools: result.toolNames,
    healthchecks: result.healthchecks,
    obscureValues: result.obscureValues || [],
  };
}
