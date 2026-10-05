import crypto from 'node:crypto';

const ALGORITHM = 'aes-256-cbc';

function deriveKey() {
  const passphrase = process.env.AGENTO_ENCRYPTION_KEY;
  if (!passphrase) return null;
  return crypto.createHash('sha256').update(passphrase).digest();
}

/**
 * Decrypt a value stored as "aes256:{iv_hex}:{ciphertext_hex}".
 * Returns plaintext string, or throws if key is missing or decryption fails.
 */
export function decrypt(encoded) {
  const key = deriveKey();
  if (!key) throw new Error('AGENTO_ENCRYPTION_KEY not set — cannot decrypt');

  const parts = encoded.split(':');
  if (parts.length !== 3 || parts[0] !== 'aes256') {
    throw new Error(`Invalid encrypted format: expected "aes256:{iv}:{ciphertext}"`);
  }

  const iv = Buffer.from(parts[1], 'hex');
  const ciphertext = Buffer.from(parts[2], 'hex');
  const decipher = crypto.createDecipheriv(ALGORITHM, key, iv);
  let plaintext = decipher.update(ciphertext, null, 'utf8');
  plaintext += decipher.final('utf8');
  return plaintext;
}

/**
 * Encrypt a plaintext value. Returns "aes256:{iv_hex}:{ciphertext_hex}".
 */
export function encrypt(plaintext) {
  const key = deriveKey();
  if (!key) throw new Error('AGENTO_ENCRYPTION_KEY not set — cannot encrypt');

  const iv = crypto.randomBytes(16);
  const cipher = crypto.createCipheriv(ALGORITHM, key, iv);
  let ciphertext = cipher.update(plaintext, 'utf8', 'hex');
  ciphertext += cipher.final('hex');
  return `aes256:${iv.toString('hex')}:${ciphertext}`;
}

/**
 * Check if AGENTO_ENCRYPTION_KEY is available.
 */
export function hasEncryptionKey() {
  return !!process.env.AGENTO_ENCRYPTION_KEY;
}
