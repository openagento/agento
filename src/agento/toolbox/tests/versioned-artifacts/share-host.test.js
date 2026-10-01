import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { validShareHost, shareUrl } from '../../../modules/versioned_artifacts/toolbox/share-host.js';

// The same fixture drives the proxy entrypoint test (SEC-5, PRD E6 §9.1).
const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(readFileSync(path.join(HERE, '../../../../../tests/fixtures/share_host_v1.json'), 'utf8'));
const TOKEN = '0123456789abcdef0123456789abcdef';

describe('share host', () => {
  it.each(FIXTURE.valid)('accepts %j', (h) => expect(validShareHost(h)).toBe(true));
  it.each(FIXTURE.invalid)('refuses %j', (h) => expect(validShareHost(h)).toBe(false));

  it('builds the URL, with the port only when it is not 443', () => {
    expect(shareUrl(TOKEN, { AGENTO_SHARE_HOST: 'share.example.com', AGENTO_PROXY_PORT: '443' }))
      .toBe(`https://${TOKEN}.share.example.com/`);
    expect(shareUrl(TOKEN, { AGENTO_SHARE_HOST: 'share.localhost', AGENTO_PROXY_PORT: '8443' }))
      .toBe(`https://${TOKEN}.share.localhost:8443/`);
  });

  it('is null for an empty host, an invalid host or port, and a bad token', () => {
    expect(shareUrl(TOKEN, { AGENTO_SHARE_HOST: '' })).toBeNull();
    expect(shareUrl(TOKEN, {})).toBeNull();
    expect(shareUrl(TOKEN, { AGENTO_SHARE_HOST: 'share.localhost', AGENTO_PROXY_PORT: '99999' })).toBeNull();
    expect(shareUrl('x', { AGENTO_SHARE_HOST: 'share.localhost' })).toBeNull();
  });

  it('warns once on an invalid host', async () => {
    vi.resetModules();
    const fresh = await import('../../../modules/versioned_artifacts/toolbox/share-host.js');
    const log = vi.fn();
    fresh.shareUrl(TOKEN, { AGENTO_SHARE_HOST: '*.x.y' }, log);
    fresh.shareUrl(TOKEN, { AGENTO_SHARE_HOST: '*.x.y' }, log);
    expect(log).toHaveBeenCalledTimes(1);
  });
});
