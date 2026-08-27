import { describe, it, expect } from 'vitest';
import { probe, required } from '../probes/smtp.js';

const CTX = { timeoutMs: 1000 };

function transportStub(verify, seen = {}) {
  return {
    createTransport: (opts) => {
      Object.assign(seen, opts);
      return { verify, close: () => { seen.closed = true; } };
    },
  };
}

describe('smtp probe', () => {
  it('declares host as its only required field', () => {
    // user/pass are optional: an SMTP relay on the internal network may take no
    // auth at all, and `app_monitor/alerts/smtp_user` is documented as
    // "empty = no auth".
    expect(required).toEqual(['host']);
  });

  it('reports ok when verify() resolves', async () => {
    const seen = {};
    const out = await probe(
      { host: 'mail', port: '587', user: 'u', pass: 'p', starttls: 'true' },
      { ...CTX, deps: transportStub(async () => true, seen) },
    );
    expect(out.status).toBe('ok');
    expect(seen.host).toBe('mail');
    expect(seen.port).toBe(587);              // parsed, not the string
    expect(seen.secure).toBe(false);          // 587 is STARTTLS, not implicit TLS
    expect(seen.requireTLS).toBe(true);
    expect(seen.auth).toEqual({ user: 'u', pass: 'p' });
    expect(seen.closed).toBe(true);           // the socket is not leaked
  });

  it('uses implicit TLS on 465 and does not ask for STARTTLS', async () => {
    const seen = {};
    await probe({ host: 'mail', port: '465' }, { ...CTX, deps: transportStub(async () => true, seen) });
    expect(seen.secure).toBe(true);
    expect(seen.requireTLS).toBe(false);
  });

  it('omits auth entirely when no user is configured', async () => {
    const seen = {};
    await probe({ host: 'mail' }, { ...CTX, deps: transportStub(async () => true, seen) });
    expect(seen.auth).toBeUndefined();
    expect(seen.port).toBe(587);              // the documented default
  });

  it('maps an authentication rejection to AUTH_FAILED', async () => {
    // The shape of the real incident: nodemailer sets code=EAUTH and
    // responseCode=535 when the server rejects LOGIN.
    const err = Object.assign(new Error('Invalid login: 535 5.7.8 authentication failed'), {
      code: 'EAUTH', responseCode: 535,
    });
    const out = await probe({ host: 'mail', user: 'u', pass: 'p' }, {
      ...CTX, deps: transportStub(async () => { throw err; }),
    });
    expect(out.status).toBe('fail');
    expect(out.code).toBe('AUTH_FAILED');
    expect(out.detail).toContain('535');
  });

  it.each([
    ['ECONNECTION', 'CONNECTION_FAILED'],
    ['ETIMEDOUT', 'CONNECTION_FAILED'],
    ['ESOCKET', 'CONNECTION_FAILED'],
    ['EDNS', 'DNS_FAILED'],
  ])('reports %s as ERROR, not as a credential verdict', async (code, expected) => {
    // The load-bearing distinction of the whole feature: the server never
    // answered, so nothing was rejected. A firewall must not read as a bad
    // password — that is what sends someone rotating a working credential.
    const err = Object.assign(new Error('connect ECONNREFUSED'), { code });
    const out = await probe({ host: 'mail' }, {
      ...CTX, deps: transportStub(async () => { throw err; }),
    });
    expect(out.status).toBe('error');
    expect(out.code).toBe(expected);
  });

  it('falls back to a generic ERROR for an unrecognised failure', async () => {
    const out = await probe({ host: 'mail' }, {
      ...CTX, deps: transportStub(async () => { throw new Error('kaboom'); }),
    });
    expect(out.status).toBe('error');
    expect(out.code).toBe('SMTP_ERROR');
  });

  it('is the ONLY path to `fail`: an EAUTH rejection', async () => {
    // Stated as a test so a later "helpfully" widened classify() breaks here.
    const eauth = Object.assign(new Error('Invalid login'), { code: 'EAUTH' });
    const out = await probe({ host: 'mail', user: 'u', pass: 'p' }, {
      ...CTX, deps: transportStub(async () => { throw eauth; }),
    });
    expect(out.status).toBe('fail');
  });

  it('never sends a message', async () => {
    // `verify()` is the whole probe: it connects, EHLOs, upgrades and logs in,
    // then quits. A transport whose sendMail is called would fail this test.
    const out = await probe({ host: 'mail' }, {
      ...CTX,
      deps: {
        createTransport: () => ({
          verify: async () => true,
          sendMail: () => { throw new Error('sendMail must never be called'); },
          close: () => {},
        }),
      },
    });
    expect(out.status).toBe('ok');
  });

  it('survives a transport with no close()', async () => {
    const out = await probe({ host: 'mail' }, {
      ...CTX, deps: { createTransport: () => ({ verify: async () => true }) },
    });
    expect(out.status).toBe('ok');
  });
});
