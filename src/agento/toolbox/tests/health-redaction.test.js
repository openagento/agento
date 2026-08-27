import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { collectObscureValues, redactSecrets } from '../config-loader.js';
import { runHealthchecks } from '../healthchecks.js';

describe('redactSecrets', () => {
  it('masks a secret that an upstream error echoed back', () => {
    // The shape of the real incident: a 535 from the mail provider, with the credential
    // quoted back by the server.
    const msg = "535 5.7.8 authentication failed for user with pass hunter2xyz";
    expect(redactSecrets(msg, ['hunter2xyz'])).toBe(
      '535 5.7.8 authentication failed for user with pass ***'
    );
  });

  it('masks the longer secret first when one contains the other', () => {
    expect(redactSecrets('ab abcd', ['ab', 'abcd'])).toBe('*** ***');
  });

  it('leaves a message with no secret in it untouched', () => {
    expect(redactSecrets('535 authentication failed', ['hunter2'])).toBe(
      '535 authentication failed'
    );
  });

  it('is a no-op for a non-string or an empty secret list', () => {
    expect(redactSecrets(undefined, ['x'])).toBe(undefined);
    expect(redactSecrets('text', [])).toBe('text');
    expect(redactSecrets('text', [''])).toBe('text');
  });
});

describe('runHealthchecks redaction', () => {
  it('redacts a returned check error', async () => {
    const checks = await runHealthchecks(
      [async () => [{ tool: 'email_send', status: 'fail', error: '535 rejected hunter2xyz' }]],
      ['hunter2xyz']
    );
    expect(checks).toEqual([
      { tool: 'email_send', status: 'fail', error: '535 rejected ***' },
    ]);
  });

  it('redacts a THROWN healthcheck message too', async () => {
    // The branch a helper-only test never reaches: an exception message is
    // assembled by server code, not by the adapter, and routinely quotes the
    // credential the client just used.
    const checks = await runHealthchecks(
      [async () => { throw new Error('connect failed for pass hunter2xyz'); }],
      ['hunter2xyz']
    );
    expect(checks).toEqual([
      { tool: 'unknown', status: 'fail', error: 'connect failed for pass ***' },
    ]);
  });

  it('passes a check with no error through unchanged', async () => {
    const checks = await runHealthchecks([async () => [{ tool: 'x', status: 'ok' }]], ['s']);
    expect(checks).toEqual([{ tool: 'x', status: 'ok' }]);
  });

  it('redacts nothing when no values were collected — and still answers', async () => {
    const checks = await runHealthchecks(
      [async () => [{ tool: 'x', status: 'fail', error: 'plain' }]]
    );
    expect(checks).toEqual([{ tool: 'x', status: 'fail', error: 'plain' }]);
  });
});

// A real directory and the injectable `modules` parameter — no mocking at all.
// `config-loader.js` does `import fs from 'fs'` and calls `fs.existsSync` /
// `fs.readFileSync` off that DEFAULT binding, so a `vi.doMock('fs', …)` that
// overrides named exports and passes `default: actual.default` through never
// intercepts anything: the test would silently read the real filesystem and pass
// for the wrong reason.
describe('collectObscureValues', () => {
  function dirWith(system) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'obscure-'));
    fs.writeFileSync(path.join(dir, 'system.json'), JSON.stringify(system));
    return dir;
  }

  it('collects exactly the resolved values of obscure fields', () => {
    const dir = dirWith({ smtp_pass: { type: 'obscure' }, smtp_host: { type: 'text' } });

    expect(collectObscureValues(
      { core: { smtp_pass: 'hunter2xyz', smtp_host: 'smtp.example.com' } },
      [{ name: 'core', _path: dir }],
    )).toEqual(['hunter2xyz']);
  });

  it('skips a module whose system.json is absent or malformed', () => {
    const bad = fs.mkdtempSync(path.join(os.tmpdir(), 'obscure-bad-'));
    fs.writeFileSync(path.join(bad, 'system.json'), '{ not json');
    const none = fs.mkdtempSync(path.join(os.tmpdir(), 'obscure-none-'));

    expect(collectObscureValues(
      { a: { smtp_pass: 'hunter2xyz' }, b: { smtp_pass: 'hunter2xyz' } },
      [{ name: 'a', _path: bad }, { name: 'b', _path: none }],
    )).toEqual([]);
  });
});
