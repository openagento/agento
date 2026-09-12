import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('node:fs', () => ({
  appendFileSync: vi.fn(),
}));

import { appendFileSync } from 'node:fs';
import { createScopedLogger, createPhasedLogger, logToolboxMcp, logToolboxRest } from '../log.js';

describe('log sinks', () => {
  beforeEach(() => {
    vi.spyOn(console, 'log').mockImplementation(() => {});
    appendFileSync.mockClear();
  });

  it('logToolboxMcp writes to the MCP log file', () => {
    logToolboxMcp('email_send', 'OK', 'sent');
    expect(appendFileSync).toHaveBeenCalledWith('/app/logs/toolbox_mcp.log', expect.any(String));
  });

  it('logToolboxRest writes to the REST log file', () => {
    logToolboxRest('api/jira/search', 'OK', 'hits=3');
    expect(appendFileSync).toHaveBeenCalledWith('/app/logs/toolbox_rest.log', expect.any(String));
  });

  it('createScopedLogger writes to the MCP log file', () => {
    const log = createScopedLogger({ id: 1, label: 'Dev' });
    log('mysql_prod', 'QUERY', 'select 1');
    expect(appendFileSync).toHaveBeenCalledWith('/app/logs/toolbox_mcp.log', expect.any(String));
  });
});

describe('createPhasedLogger', () => {
  beforeEach(() => {
    vi.spyOn(console, 'log').mockImplementation(() => {});
    appendFileSync.mockClear();
  });

  it('routes registration-phase logging to the REST file', () => {
    const invocationLog = vi.fn();
    const log = createPhasedLogger(invocationLog);
    log('browser', 'INIT', 'whitelist=[...]');
    expect(appendFileSync).toHaveBeenCalledWith('/app/logs/toolbox_rest.log', expect.any(String));
    expect(invocationLog).not.toHaveBeenCalled();
  });

  it('routes invocation-phase logging to the injected MCP logger after the flip', () => {
    const invocationLog = vi.fn();
    const log = createPhasedLogger(invocationLog);
    log.toInvocationPhase();
    log('email_send', 'OK', 'sent');
    expect(invocationLog).toHaveBeenCalledWith('email_send', 'OK', 'sent');
    // nothing new written to the REST file during the invocation phase
    expect(appendFileSync).not.toHaveBeenCalled();
  });

  it('uses the real MCP file when the injected logger is logToolboxMcp', () => {
    const log = createPhasedLogger(logToolboxMcp);
    log('discovery', 'OK', 'registered'); // registration phase -> REST
    log.toInvocationPhase();
    log('mysql_prod', 'OK', 'rows=1');     // invocation phase -> MCP
    expect(appendFileSync).toHaveBeenCalledWith('/app/logs/toolbox_rest.log', expect.any(String));
    expect(appendFileSync).toHaveBeenCalledWith('/app/logs/toolbox_mcp.log', expect.any(String));
  });
});

describe('createScopedLogger', () => {
  beforeEach(() => {
    vi.spyOn(console, 'log').mockImplementation(() => {});
  });

  it('includes agent_view prefix when meta provided', () => {
    const log = createScopedLogger({ id: 42, label: 'Developer' });
    log('mysql_prod', 'OK', 'rows=1');
    const output = console.log.mock.calls[0][0];
    expect(output).toContain('[Developer (id: 42)]');
    expect(output).toContain('[mysql_prod]');
    expect(output).toContain('OK');
    expect(output).toContain('rows=1');
  });

  it('omits prefix when no meta', () => {
    const log = createScopedLogger(null);
    log('mysql_prod', 'OK', 'rows=1');
    const output = console.log.mock.calls[0][0];
    expect(output).not.toContain('(id:');
    expect(output).toContain('[mysql_prod]');
  });

  it('includes ISO timestamp', () => {
    const log = createScopedLogger({ id: 1, label: 'Test' });
    log('tool', 'OK');
    const output = console.log.mock.calls[0][0];
    // ISO timestamp pattern: [2026-03-29T...]
    expect(output).toMatch(/\[\d{4}-\d{2}-\d{2}T/);
  });

  it('handles empty details', () => {
    const log = createScopedLogger({ id: 1, label: 'Test' });
    log('tool', 'OK');
    const output = console.log.mock.calls[0][0];
    expect(output).toContain('[tool] OK ');
  });
});

// A log record is one physical line, and every sink builds it by concatenating
// caller-supplied `details`. Without sanitizing there, any caller that puts a
// newline in `details` writes a SECOND record the operator cannot distinguish
// from a genuine one - so the guard belongs in the shared writer, not in the
// ~200 call sites.
describe('a caller cannot forge a log record', () => {
  const FORGERY = 'bad\n[api/jira/request] OK forged';

  beforeEach(() => {
    vi.spyOn(console, 'log').mockImplementation(() => {});
    appendFileSync.mockClear();
  });

  const written = () => appendFileSync.mock.calls[0][1];

  it.each([
    ['logToolboxRest', (d) => logToolboxRest('api/jira/request', 'ERROR', d)],
    ['logToolboxMcp', (d) => logToolboxMcp('some_tool', 'ERROR', d)],
    ['createScopedLogger', (d) => createScopedLogger({ id: 1, label: 'Dev' })('some_tool', 'ERROR', d)],
  ])('%s writes exactly one line for details containing a newline', (_name, emit) => {
    emit(FORGERY);

    const record = written();
    expect(record.endsWith('\n')).toBe(true);
    expect(record.trimEnd().split('\n')).toHaveLength(1);
    expect(record).toContain('forged');          // kept as text, not as a record
  });

  it('strips control characters that no operator can read or grep', () => {
    logToolboxRest('api/jira/request', 'ERROR', 'a\u0000b\u001b[31mc');

    expect(written()).not.toMatch(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/);
  });

  it('bounds a flooded detail', () => {
    logToolboxRest('api/jira/request', 'ERROR', 'x'.repeat(10_000));

    expect(written().length).toBeLessThan(2_500);
  });
});
