import { describe, it, expect } from 'vitest';
import { configTests, healthcheck } from '../../modules/outlook/toolbox/credentials.js';

const FULL = {
  outlook_tenant_id: 't', outlook_client_id: 'c',
  outlook_mailbox_user_id: 'shared@example.com',
  outlook_client_secret: 'super-secret-value',
};

const entry = () => configTests.find((e) => e.name === 'graph_credentials');

function deps(getToken) {
  return { graphAuthFactory: () => ({ isConfigured: () => true, getToken }) };
}

describe('outlook graph_credentials — the named config-test probe', () => {
  it('is declared with a name, its paths, and a run function', () => {
    const e = entry();
    expect(typeof e.run).toBe('function');
    expect(e.paths).toContain('outlook/outlook_tenant_id');
    expect(e.paths).toContain('outlook/outlook_client_secret');
  });

  it('declares only outlook paths', () => {
    // runConfigTest refuses a foreign path with FOREIGN_PATH; failing here is
    // cheaper than failing at runtime.
    for (const p of entry().paths) expect(p.startsWith('outlook/')).toBe(true);
  });

  it('returns ok when a token is acquired', async () => {
    const out = await entry().run({ config: FULL, deps: deps(async () => 'tok') });
    expect(out.status).toBe('ok');
  });

  it('takes the config keyed by field name, unchanged', async () => {
    // The framework hands { outlook_tenant_id: … }, which is what
    // createGraphAuth already expects. No adapter layer in between.
    let seen = null;
    await entry().run({
      config: FULL,
      deps: { graphAuthFactory: (c) => { seen = c; return { isConfigured: () => true, getToken: async () => 'tok' }; } },
    });
    expect(seen.outlook_tenant_id).toBe('t');
  });

  it('returns fail — not error — when the credential is rejected', async () => {
    const out = await entry().run({
      config: FULL,
      deps: deps(async () => { throw new Error('Graph token acquisition failed (invalid_client)'); }),
    });
    expect(out.status).toBe('fail');
    expect(out.code).toBe('INVALID_CLIENT');
    expect(out.detail).toContain('invalid_client');
  });

  it('gives a shaped code even when the provider message has none', async () => {
    const out = await entry().run({
      config: FULL, deps: deps(async () => { throw new Error('something odd'); }),
    });
    expect(out.status).toBe('fail');
    expect(out.code).toBe('TOKEN_FAILED');
  });

  it.each([
    'getaddrinfo ENOTFOUND login.microsoftonline.com',
    'connect ECONNREFUSED 10.0.0.1:443',
    'Graph token acquisition failed (network_error)',
    'Graph token acquisition failed (request_timeout)',
  ])('reports a transport failure as error, not as a rejected credential: %s', async (msg) => {
    // The provider never answered, so it rejected nothing. Same rule as the
    // smtp and http probes — `fail` is a verdict and only Entra can give one.
    const out = await entry().run({
      config: FULL, deps: deps(async () => { throw new Error(msg); }),
    });
    expect(out.status).toBe('error');
  });

  it('never puts the credential in the detail', async () => {
    const out = await entry().run({
      config: FULL,
      deps: deps(async () => { throw new Error('rejected super-secret-value'); }),
    });
    expect(out.detail).not.toContain('super-secret-value');
  });

  it('returns not_configured when isConfigured is false', async () => {
    // NOT fail: nobody set this up, and a red FAIL on an integration a
    // deployment does not use is noise that trains people to ignore the button.
    const out = await entry().run({
      config: {},
      deps: { graphAuthFactory: () => ({ isConfigured: () => false, getToken: async () => 'x' }) },
    });
    expect(out.status).toBe('not_configured');
  });

  it('says what is missing when it is not configured', async () => {
    const out = await entry().run({
      config: {},
      deps: { graphAuthFactory: () => ({ isConfigured: () => false, getToken: async () => 'x' }) },
    });
    expect(out.detail).toMatch(/tenant/i);
  });

  it('tests the certificate path through the same call', async () => {
    let seen = null;
    const cfg = {
      outlook_tenant_id: 't', outlook_client_id: 'c',
      outlook_mailbox_user_id: 'm@example.com',
      outlook_cert_pem: '-----BEGIN CERTIFICATE-----',
      outlook_cert_password: 'pw',
    };
    const out = await entry().run({
      config: cfg,
      deps: { graphAuthFactory: (c) => { seen = c; return { isConfigured: () => true, getToken: async () => 'tok' }; } },
    });
    expect(out.status).toBe('ok');
    expect(seen.outlook_cert_pem).toBeTruthy();
    expect(seen.outlook_cert_password).toBe('pw');
  });

  it('uses the real factory when no deps are injected', async () => {
    // An empty config must reach not_configured through createGraphAuth itself,
    // not only through a stub — otherwise the tests prove nothing about the
    // wiring the toolbox actually runs.
    const out = await entry().run({ config: {} });
    expect(out.status).toBe('not_configured');
  });
});

describe('outlook healthcheck — the /health entry over the same probe', () => {
  it('reports ok when a token is acquired', async () => {
    const out = await healthcheck({ moduleConfigs: { outlook: FULL } }, deps(async () => 'tok'));
    expect(out).toHaveLength(1);
    expect(out[0].tool).toBe('outlook');
    expect(out[0].status).toBe('ok');
  });

  it('maps a rejected credential to fail', async () => {
    const out = await healthcheck(
      { moduleConfigs: { outlook: FULL } },
      deps(async () => { throw new Error('Graph token acquisition failed (invalid_client)'); }),
    );
    expect(out[0].status).toBe('fail');
    expect(out[0].error).toContain('invalid_client');
  });

  it('maps not_configured to skip, the healthcheck vocabulary', async () => {
    const out = await healthcheck(
      { moduleConfigs: { outlook: {} } },
      { graphAuthFactory: () => ({ isConfigured: () => false, getToken: async () => 'x' }) },
    );
    expect(out[0].status).toBe('skip');
  });

  it('never puts the credential in the error', async () => {
    const out = await healthcheck(
      { moduleConfigs: { outlook: FULL } },
      deps(async () => { throw new Error('rejected super-secret-value'); }),
    );
    expect(out[0].error).not.toContain('super-secret-value');
  });
});
