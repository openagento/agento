import { describe, it, expect, vi, beforeEach } from 'vitest';

describe('browser tools', () => {
  let mockClient;
  let mockReadFile;
  let mockWriteFile;
  let mockMkdir;
  let mockCopyFile;
  let mockUnlink;

  beforeEach(() => {
    vi.resetModules();
    mockClient = {
      callTool: vi.fn().mockResolvedValue({ content: [{ type: 'text', text: 'ok' }] }),
      listTools: vi.fn().mockResolvedValue({ tools: [] }),
    };
    mockReadFile = vi.fn().mockRejectedValue(new Error('ENOENT'));
    mockWriteFile = vi.fn().mockResolvedValue(undefined);
    mockMkdir = vi.fn().mockResolvedValue(undefined);
    mockCopyFile = vi.fn().mockResolvedValue(undefined);
    mockUnlink = vi.fn().mockResolvedValue(undefined);
  });

  function setupMocks() {
    vi.doMock('fs/promises', () => ({
      readFile: mockReadFile,
      writeFile: mockWriteFile,
      mkdir: mockMkdir,
      copyFile: mockCopyFile,
      unlink: mockUnlink,
    }));
  }

  function buildContext(whitelistTools) {
    return {
      log: vi.fn(),
      playwright: {
        getClient: () => mockClient,
        getTools: () => [],
        getViewport: () => ({ width: 1366, height: 768 }),
      },
      moduleConfigs: {
        core: {
          allowed_domains: 'example.com',
        },
      },
      // Membership is per-tool enablement now — there is no whitelist.
      isToolEnabled: (name) => whitelistTools.includes(name),
      artifactsDir: '/workspace/tmp',
    };
  }

  async function importAndRegister(whitelistTools) {
    setupMocks();
    const { register } = await import('../../modules/core/toolbox/browser.js');
    const handlers = {};
    const server = {
      tool: vi.fn((name, _desc, _schema, handler) => {
        handlers[name] = handler;
      }),
    };
    const context = buildContext(whitelistTools);
    register(server, context);
    return { handlers, server, context };
  }

  describe('per-tool enablement (no module-level gate, no whitelist)', () => {
    it('gates each browser tool on its own key and never queries the "browser" master', async () => {
      // The framework applies the master via each tool's requires:, so browser.js must ask
      // per tool. Asserting only "nothing registered" would also pass with the old
      // module-level early-return, which is exactly what this change removes.
      setupMocks();
      const { register } = await import('../../modules/core/toolbox/browser.js');
      const asked = [];
      const server = { tool: vi.fn() };
      const context = buildContext([]);
      register(server, { ...context, isToolEnabled: (n) => { asked.push(n); return false; } });

      expect(asked).not.toContain('browser');
      expect(asked).toContain('browser_navigate');
      expect(asked.every(n => n.startsWith('browser_'))).toBe(true);
      expect(server.tool).not.toHaveBeenCalled();
    });

    it('keeps cookie-injection state per session, not per process', async () => {
      // register() runs once per MCP session but the module is imported once per process, so a
      // module-level Set would leak one agent_view's injected domains into another's session.
      // The cookie branch only runs when session.json HAS a matching cookie — with the default
      // rejecting readFile this test would pass even with the old module-level Set.
      mockReadFile = vi.fn().mockResolvedValue(JSON.stringify({
        cookies: [{ name: 'sid', value: 'abc', domain: 'example.com', path: '/' }],
      }));
      setupMocks();
      const { register } = await import('../../modules/core/toolbox/browser.js');

      const runSession = async () => {
        const handlers = {};
        const server = { tool: vi.fn((name, _d, _s, handler) => { handlers[name] = handler; }) };
        register(server, buildContext(['browser_navigate']));
        await handlers.browser_navigate({ user: 'a@b.com', url: 'https://example.com/page' });
        return mockClient.callTool.mock.calls.map(c => c[0]);
      };

      const first = await runSession();
      // The cookie hop: navigate to robots.txt, then set document.cookie via evaluate.
      expect(first.filter(c => c.name === 'browser_navigate')
        .some(c => c.arguments.url.endsWith('/robots.txt'))).toBe(true);
      expect(first.some(c => c.name === 'browser_evaluate')).toBe(true);

      mockClient.callTool.mockClear();
      const second = await runSession();

      // A fresh session must redo the hop rather than inherit the first session's bookkeeping.
      expect(second.map(c => c.name)).toEqual(first.map(c => c.name));
      expect(second.filter(c => c.name === 'browser_navigate')
        .some(c => c.arguments.url.endsWith('/robots.txt'))).toBe(true);
    });

    it('registers only the tools the gate allows', async () => {
      const { server } = await importAndRegister(['browser_snapshot']);
      expect(server.tool.mock.calls.map(c => c[0])).toEqual(['browser_snapshot']);
    });
  });

  describe('browser_start_video', () => {
    it('forwards size params restructured into size object', async () => {
      const { handlers } = await importAndRegister(['browser_start_video']);

      await handlers.browser_start_video({
        user: 'a@b.com',
        width: 1280,
        height: 720,
      });

      expect(mockClient.callTool).toHaveBeenCalledWith({
        name: 'browser_start_video',
        arguments: { size: { width: 1280, height: 720 } },
      });
    });

    it('defaults the recording size to the browser viewport when width/height are omitted', async () => {
      const { handlers } = await importAndRegister(['browser_start_video']);

      await handlers.browser_start_video({ user: 'a@b.com' });

      expect(mockClient.callTool).toHaveBeenCalledWith({
        name: 'browser_start_video',
        arguments: { size: { width: 1366, height: 768 } },
      });
    });

    it('fills the missing dimension from the viewport when only one is provided', async () => {
      const { handlers } = await importAndRegister(['browser_start_video']);

      await handlers.browser_start_video({ user: 'a@b.com', width: 1920 });

      expect(mockClient.callTool).toHaveBeenCalledWith({
        name: 'browser_start_video',
        arguments: { size: { width: 1920, height: 768 } },
      });
    });
  });

  describe('browser_stop_video', () => {
    it('parses video path, copies to runtime dir, cleans up source', async () => {
      mockClient.callTool.mockResolvedValue({
        content: [{ type: 'text', text: '- [Video](/tmp/pw-video/video.webm)' }],
      });

      const { handlers } = await importAndRegister(['browser_stop_video']);

      const result = await handlers.browser_stop_video({
        user: 'a@b.com',
        job_id: '42',
      });

      expect(mockMkdir).toHaveBeenCalledWith('/workspace/tmp/videos/42', { recursive: true });
      expect(mockCopyFile).toHaveBeenCalledWith(
        expect.stringContaining('/tmp/pw-video/video.webm'),
        expect.stringMatching(/^\/workspace\/tmp\/videos\/42\/\d+\.webm$/),
      );
      expect(mockUnlink).toHaveBeenCalled();
      expect(result.content[0].text).toMatch(/^Video saved to: \/workspace\/tmp\/videos\/42\/\d+\.webm$/);
    });

    it('uses custom filename when provided', async () => {
      mockClient.callTool.mockResolvedValue({
        content: [{ type: 'text', text: '- [Video](/tmp/pw-video/video.webm)' }],
      });

      const { handlers } = await importAndRegister(['browser_stop_video']);

      const result = await handlers.browser_stop_video({
        user: 'a@b.com',
        job_id: '42',
        filename: 'demo.webm',
      });

      expect(mockCopyFile).toHaveBeenCalledWith(
        expect.stringContaining('/tmp/pw-video/video.webm'),
        '/workspace/tmp/videos/42/demo.webm',
      );
      expect(result.content[0].text).toBe('Video saved to: /workspace/tmp/videos/42/demo.webm');
    });

    it('matches video links with dimensions in text (e.g. [Video 1280x720])', async () => {
      mockClient.callTool.mockResolvedValue({
        content: [{ type: 'text', text: '- [Video 1280x720](/tmp/pw/recording.webm)' }],
      });

      const { handlers } = await importAndRegister(['browser_stop_video']);

      const result = await handlers.browser_stop_video({
        user: 'a@b.com',
        filename: 'out.webm',
      });

      expect(mockCopyFile).toHaveBeenCalled();
      expect(result.content[0].text).toMatch(/Video saved to:/);
    });

    it('returns raw result when no video files in response', async () => {
      mockClient.callTool.mockResolvedValue({
        content: [{ type: 'text', text: 'No recording active' }],
      });

      const { handlers } = await importAndRegister(['browser_stop_video']);

      const result = await handlers.browser_stop_video({
        user: 'a@b.com',
        job_id: '42',
      });

      expect(mockCopyFile).not.toHaveBeenCalled();
      expect(result).toEqual({
        content: [{ type: 'text', text: 'No recording active' }],
      });
    });
  });

  describe('playwright not connected', () => {
    async function registerWithState(stateObj) {
      setupMocks();
      const { register } = await import('../../modules/core/toolbox/browser.js');
      const handlers = {};
      const server = {
        tool: vi.fn((name, _desc, _schema, handler) => {
          handlers[name] = handler;
        }),
      };
      const context = {
        log: vi.fn(),
        playwright: {
          getClient: () => null,
          getTools: () => [],
          getState: () => stateObj,
        },
        moduleConfigs: {
          core: {
            allowed_domains: 'example.com',
          },
        },
        isToolEnabled: (name) => name === 'browser_snapshot',
        artifactsDir: '/workspace/tmp',
      };
      register(server, context);
      return handlers;
    }

    it('returns "starting up" error when state=starting', async () => {
      const handlers = await registerWithState({ state: 'starting', attempt: 0, maxAttempts: 5, lastError: null });
      const result = await handlers.browser_snapshot({ user: 'a@b.com' });
      expect(result.isError).toBe(true);
      expect(result.content[0].text).toContain('starting up');
      expect(result.content[0].text).not.toContain('permanently failed');
    });

    it('returns "restarting" error with attempt N of M when state=restarting', async () => {
      const handlers = await registerWithState({ state: 'restarting', attempt: 2, maxAttempts: 5, lastError: 'EPIPE' });
      const result = await handlers.browser_snapshot({ user: 'a@b.com' });
      expect(result.isError).toBe(true);
      expect(result.content[0].text).toContain('restarting (attempt 2 of 5)');
      expect(result.content[0].text).not.toContain('starting up');
    });

    it('returns "permanently failed" error with lastError when state=failed', async () => {
      const handlers = await registerWithState({ state: 'failed', attempt: 5, maxAttempts: 5, lastError: 'Chromium SIGSEGV' });
      const result = await handlers.browser_snapshot({ user: 'a@b.com' });
      expect(result.isError).toBe(true);
      expect(result.content[0].text).toContain('failed permanently after 5');
      expect(result.content[0].text).toContain('Chromium SIGSEGV');
    });
  });

  describe('no cross-session leak in register()', () => {
    it('passthrough tool list logged is identical across N register() calls', async () => {
      setupMocks();
      const { register } = await import('../../modules/core/toolbox/browser.js');

      const fakeUpstream = [
        { name: 'browser_fake_one', description: 'a', inputSchema: { type: 'object', properties: {} } },
        { name: 'browser_fake_two', description: 'b', inputSchema: { type: 'object', properties: {} } },
      ];
      const whitelist = fakeUpstream.map(t => t.name);

      const logs = [];
      function buildContext() {
        return {
          log: (mod, level, msg) => logs.push({ mod, level, msg }),
          playwright: {
            getClient: () => mockClient,
            getTools: () => fakeUpstream,
          },
          moduleConfigs: {
            core: {
              allowed_domains: 'example.com',
            },
          },
          isToolEnabled: (name) => whitelist.includes(name),
          artifactsDir: '/workspace/tmp',
        };
      }

      const N = 10;
      for (let i = 0; i < N; i++) {
        const server = { tool: vi.fn() };
        register(server, buildContext());
      }

      const passthroughLogs = logs.filter(
        l => l.mod === 'browser' && l.level === 'INIT' && l.msg.startsWith('Passthrough tools registered:'),
      );
      expect(passthroughLogs).toHaveLength(N);

      const firstMsg = passthroughLogs[0].msg;
      for (const log of passthroughLogs) {
        expect(log.msg).toBe(firstMsg);
      }
    });

    it('each register() call only registers its own tools on its own server', async () => {
      setupMocks();
      const { register } = await import('../../modules/core/toolbox/browser.js');

      const fakeUpstream = [
        { name: 'browser_fake_one', description: 'a', inputSchema: { type: 'object', properties: {} } },
      ];
      const whitelist = fakeUpstream.map(t => t.name);

      function buildContext() {
        return {
          log: vi.fn(),
          playwright: {
            getClient: () => mockClient,
            getTools: () => fakeUpstream,
          },
          moduleConfigs: {
            core: {
              allowed_domains: 'example.com',
            },
          },
          isToolEnabled: (name) => whitelist.includes(name),
          artifactsDir: '/workspace/tmp',
        };
      }

      const servers = [];
      for (let i = 0; i < 5; i++) {
        const server = { tool: vi.fn() };
        register(server, buildContext());
        servers.push(server);
      }

      const firstCallCount = servers[0].tool.mock.calls.length;
      expect(firstCallCount).toBeGreaterThan(0);
      for (const server of servers) {
        expect(server.tool.mock.calls.length).toBe(firstCallCount);
      }
    });
  });

  describe('healthcheck', () => {
    it('returns ok when client.listTools succeeds within timeout', async () => {
      setupMocks();
      const { healthcheck } = await import('../../modules/core/toolbox/browser.js');

      const results = await healthcheck({
        playwright: { getClient: () => mockClient },
      });

      expect(mockClient.listTools).toHaveBeenCalled();
      expect(results).toHaveLength(1);
      expect(results[0]).toMatchObject({ tool: 'browser', status: 'ok' });
      expect(results[0].ms).toBeTypeOf('number');
    });

    it('returns fail when client.listTools times out (real timer, 3.1s)', async () => {
      // listTools returns a Promise that never resolves — exercises the
      // 3000ms timeout race inside healthcheck() with real timers, since
      // mixing fake timers with Promises here is flaky.
      const hangingClient = { listTools: vi.fn(() => new Promise(() => {})) };
      setupMocks();
      const { healthcheck } = await import('../../modules/core/toolbox/browser.js');

      const results = await healthcheck({
        playwright: { getClient: () => hangingClient },
      });

      expect(results[0]).toMatchObject({ tool: 'browser', status: 'fail' });
      expect(results[0].error).toMatch(/timeout/i);
    }, 6000);

    it('returns state-aware fail when getClient is null and state=failed', async () => {
      setupMocks();
      const { healthcheck } = await import('../../modules/core/toolbox/browser.js');

      const results = await healthcheck({
        playwright: {
          getClient: () => null,
          getState: () => ({ state: 'failed', attempt: 5, maxAttempts: 5, lastError: 'boom' }),
        },
      });

      expect(results[0]).toMatchObject({ tool: 'browser', status: 'fail' });
      expect(results[0].error).toContain('failed permanently');
    });

    it('falls back to generic error when getState is not provided', async () => {
      setupMocks();
      const { healthcheck } = await import('../../modules/core/toolbox/browser.js');

      const results = await healthcheck({
        playwright: { getClient: () => null },
      });

      expect(results[0]).toEqual({
        tool: 'browser',
        status: 'fail',
        error: 'Playwright MCP not connected',
      });
    });
  });
});
