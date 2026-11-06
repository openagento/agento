import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { capabilityContext } from './capability-rows.js';
import { buildRegistration } from '../tool-registration.js';

// buildRegistration is the one place that hands every tool its invocation logger, for MCP
// sessions and the invoke route alike. A tool line must name the verified caller there, with
// a view and without one, so no tool has to log an identity itself.

const { registerTools, loadScopedDbOverridesStrict, loadWorkspaceOverridesStrict } = vi.hoisted(() => ({
  loadScopedDbOverridesStrict: vi.fn(async () => ({ overrides: {}, agentViewMeta: { id: 1, label: 'Mieszko' } })),
  loadWorkspaceOverridesStrict: vi.fn(async () => ({})),
  // One tool that logs through the session logger, as every shipped tool does.
  registerTools: vi.fn(async (server, context) => {
    server.tool('t', 'test tool', {}, async () => { context.log('t', 'OK', 'done'); });
    return { tools: new Map(), healthchecks: [] };
  }),
}));
vi.mock('../config-loader.js', () => ({ registerTools, loadScopedDbOverridesStrict, loadWorkspaceOverridesStrict }));

async function lineOfOneCall(authContext) {
  const tools = new Map();
  const server = { tool: (name, _desc, _schema, handler) => tools.set(name, handler) };
  await buildRegistration(server, authContext, { context: {}, dbQuery: vi.fn(async () => [[]]) });
  console.log.mockClear();
  await tools.get('t')({});
  return console.log.mock.calls[0][0];
}

describe('buildRegistration hands tools the verified caller logger', () => {
  beforeEach(() => { vi.spyOn(console, 'log').mockImplementation(() => {}); });
  afterEach(() => { vi.restoreAllMocks(); });

  it('names the panel user of a miniapp capability, with the view', async () => {
    const ctx = capabilityContext('miniapp', { agent_view_id: 1, actor: 'user', subject_id: '42', job_id: null });
    expect(await lineOfOneCall(ctx)).toContain('[Mieszko (id: 1)] [actor=user subject=42 job=-] [t] OK');
  });

  it('names the service of a viewless capability, with no view part', async () => {
    const line = await lineOfOneCall(capabilityContext('internal_rest', { agent_view_id: null }));
    expect(line).toMatch(/^\[[^\]]+\] \[actor=service subject=service:test job=-\] \[t\] OK/);
    expect(loadWorkspaceOverridesStrict).toHaveBeenCalled();
  });
});
