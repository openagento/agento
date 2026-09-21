import { describe, it, expect } from 'vitest';
import {
  generatePassword, defaultAuthUser, hashSecret, verifyCredential,
} from '../../../modules/versioned_artifacts/toolbox/auth.js';

describe('generatePassword', () => {
  it('is long, url-safe and different every time', () => {
    const a = generatePassword();
    const b = generatePassword();
    expect(a).toMatch(/^[A-Za-z0-9_-]{20,}$/);
    expect(a).not.toBe(b);
  });
});

describe('defaultAuthUser', () => {
  it('is the artifact code', () => {
    expect(defaultAuthUser('openagento-website')).toBe('openagento-website');
  });
});

describe('hashSecret / verifyCredential', () => {
  it('carries the hash and its parameters, never the password', () => {
    const sidecar = { user: 'site', ...hashSecret('s3cret') };
    expect(sidecar.algo).toBe('scrypt');
    expect(JSON.stringify(sidecar)).not.toContain('s3cret');
    expect(sidecar.salt).toMatch(/^[0-9a-f]+$/);
    expect(sidecar.hash).toMatch(/^[0-9a-f]+$/);
  });

  it('accepts the right user and password and rejects everything else', () => {
    const sidecar = { user: 'site', ...hashSecret('s3cret') };
    expect(verifyCredential(sidecar, 'site', 's3cret')).toBe(true);
    expect(verifyCredential(sidecar, 'site', 'wrong')).toBe(false);
    expect(verifyCredential(sidecar, 'other', 's3cret')).toBe(false);
    expect(verifyCredential(sidecar, 'site', '')).toBe(false);
  });

  it('is stable across a serialize/parse round trip, the way the server reads it', () => {
    const sidecar = JSON.parse(JSON.stringify({ user: 'u', ...hashSecret('pw with : colon') }));
    expect(verifyCredential(sidecar, 'u', 'pw with : colon')).toBe(true);
  });

  it('fails closed on a malformed or non-scrypt sidecar', () => {
    expect(verifyCredential(null, 'u', 'p')).toBe(false);
    expect(verifyCredential({}, 'u', 'p')).toBe(false);
    expect(verifyCredential({ algo: 'invalid' }, 'u', 'p')).toBe(false);
    expect(verifyCredential({ user: 'u', algo: 'scrypt', salt: 'aa', hash: '' }, 'u', 'p')).toBe(false);
  });
});
