import { beforeEach, describe, expect, it, vi } from 'vitest';

describe('health registration scope isolation', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('resolves default health independently after a scoped registration', async () => {
    const registerTools = vi.fn(async (_server, _context, agentViewId) => ({
      toolNames: [agentViewId ? `scoped_${agentViewId}` : 'default_tool'],
      healthchecks: [agentViewId ? 'scoped_check' : 'default_check'],
      // Omitted for the default scope on purpose: that is what proves the
      // `|| []` fallback rather than assuming it.
      ...(agentViewId ? { obscureValues: ['hunter2xyz'] } : {}),
    }));
    const loadScopedDbOverrides = vi.fn().mockResolvedValue({
      overrides: { scoped: true },
      agentViewMeta: { id: 7, agentViewCode: 'reviewer' },
    });
    vi.doMock('../config-loader.js', () => ({ registerTools, loadScopedDbOverrides }));

    const { createHealthRegistration } = await import('../health-registration.js');
    const defaultLog = vi.fn();
    const context = { log: defaultLog, sqlPoolRegistry: {} };

    const scoped = await createHealthRegistration(7, context);
    const defaultScope = await createHealthRegistration(null, context);

    expect(scoped).toEqual({
      tools: ['scoped_7'], healthchecks: ['scoped_check'], obscureValues: ['hunter2xyz'],
    });
    expect(defaultScope).toEqual({
      tools: ['default_tool'], healthchecks: ['default_check'], obscureValues: [],
    });
    // /health is a REST/ops path and never invokes tools, so it must NOT swap in the MCP
    // logger — both scoped and default registrations keep the caller's (REST) context.log,
    // keeping toolbox_mcp.log free of health-probe registration noise.
    expect(registerTools.mock.calls[0][1].log).toBe(defaultLog);
    expect(registerTools.mock.calls[1][1].log).toBe(defaultLog);
    expect(registerTools.mock.calls.map(call => call[2])).toEqual([7, null]);
  });
});
