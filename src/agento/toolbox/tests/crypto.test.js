import { describe, it, expect, afterEach } from 'vitest';

describe('crypto', () => {
  const origKey = process.env.AGENTO_ENCRYPTION_KEY;

  afterEach(() => {
    if (origKey === undefined) delete process.env.AGENTO_ENCRYPTION_KEY;
    else process.env.AGENTO_ENCRYPTION_KEY = origKey;
  });

  it('encrypt and decrypt roundtrip', async () => {
    process.env.AGENTO_ENCRYPTION_KEY = 'test-secret-key-for-unit-tests';
    const { encrypt, decrypt } = await import('../crypto.js');

    const plaintext = 'my-database-password-123!';
    const encrypted = encrypt(plaintext);

    expect(encrypted).toMatch(/^aes256s:[0-9a-f]+:[0-9a-f]+:[0-9a-f]+$/);
    expect(encrypted).not.toContain(plaintext);

    const decrypted = decrypt(encrypted);
    expect(decrypted).toBe(plaintext);
  });

  it('different encryptions produce different ciphertexts (random IV)', async () => {
    process.env.AGENTO_ENCRYPTION_KEY = 'test-key';
    const { encrypt } = await import('../crypto.js');

    const a = encrypt('same-value');
    const b = encrypt('same-value');
    expect(a).not.toBe(b); // different salt + IV
  });

  it('throws when AGENTO_ENCRYPTION_KEY not set', async () => {
    delete process.env.AGENTO_ENCRYPTION_KEY;

    // Need fresh import since crypto.js reads env at call time
    const { encrypt, decrypt } = await import('../crypto.js');

    expect(() => encrypt('test')).toThrow('AGENTO_ENCRYPTION_KEY not set');
    expect(() => decrypt('aes256:aa:bb')).toThrow('AGENTO_ENCRYPTION_KEY not set');
  });

  it('decrypts a legacy pre-scrypt value', async () => {
    process.env.AGENTO_ENCRYPTION_KEY = 'legacy-key';
    const nodeCrypto = (await import('crypto')).default;
    const { decrypt } = await import('../crypto.js');

    const key = nodeCrypto.createHash('sha256').update('legacy-key').digest();
    const iv = nodeCrypto.randomBytes(16);
    const cipher = nodeCrypto.createCipheriv('aes-256-cbc', key, iv);
    let ct = cipher.update('old-token', 'utf8', 'hex');
    ct += cipher.final('hex');

    expect(decrypt(`aes256:${iv.toString('hex')}:${ct}`)).toBe('old-token');
  });

  it('throws on invalid format', async () => {
    process.env.AGENTO_ENCRYPTION_KEY = 'test-key';
    const { decrypt } = await import('../crypto.js');

    expect(() => decrypt('not-encrypted')).toThrow('Invalid encrypted format');
    expect(() => decrypt('wrong:format')).toThrow('Invalid encrypted format');
  });

  it('hasEncryptionKey returns correct boolean', async () => {
    const { hasEncryptionKey } = await import('../crypto.js');

    process.env.AGENTO_ENCRYPTION_KEY = 'test';
    expect(hasEncryptionKey()).toBe(true);

    delete process.env.AGENTO_ENCRYPTION_KEY;
    expect(hasEncryptionKey()).toBe(false);
  });
});
