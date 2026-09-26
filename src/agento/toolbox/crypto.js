import crypto from 'crypto';

const ALGORITHM = 'aes-256-cbc';

// Mirrors src/agento/framework/crypto.py — changing either side alone makes every
// stored value undecryptable by the other container.
const SCRYPT = { N: 1 << 14, r: 8, p: 1, maxmem: 64 * 1024 * 1024 };
const SALT_BYTES = 16;

const keyCache = new Map();

function passphrase() {
  const value = process.env.AGENTO_ENCRYPTION_KEY;
  if (!value) throw new Error('AGENTO_ENCRYPTION_KEY not set — cannot encrypt/decrypt');
  return value;
}

/** scrypt key derivation, cached — a request may decrypt many values. */
function scryptKey(salt) {
  const secret = passphrase();
  const cacheKey = `${secret}:${salt.toString('hex')}`;
  let key = keyCache.get(cacheKey);
  if (!key) {
    key = crypto.scryptSync(secret, salt, 32, SCRYPT);
    keyCache.set(cacheKey, key);
  }
  return key;
}

/**
 * Decrypt "aes256s:{salt_hex}:{iv_hex}:{ciphertext_hex}", or the legacy
 * "aes256:{iv_hex}:{ciphertext_hex}" written before the scrypt rekey.
 */
export function decrypt(encoded) {
  const parts = encoded.split(':');
  let key, iv, ciphertext;

  if (parts[0] === 'aes256s' && parts.length === 4) {
    key = scryptKey(Buffer.from(parts[1], 'hex'));
    iv = Buffer.from(parts[2], 'hex');
    ciphertext = Buffer.from(parts[3], 'hex');
  } else if (parts[0] === 'aes256' && parts.length === 3) {
    // OBSOLETE — remove in 0.18.0. Read-only path for values the core/RekeyToScrypt
    // data patch has not rewritten yet.
    key = crypto.createHash('sha256').update(passphrase()).digest(); // codeql[js/insufficient-password-hash]
    iv = Buffer.from(parts[1], 'hex');
    ciphertext = Buffer.from(parts[2], 'hex');
  } else {
    throw new Error('Invalid encrypted format: expected "aes256s:{salt}:{iv}:{ciphertext}"');
  }

  const decipher = crypto.createDecipheriv(ALGORITHM, key, iv);
  let plaintext = decipher.update(ciphertext, null, 'utf8');
  plaintext += decipher.final('utf8');
  return plaintext;
}

/**
 * Encrypt a plaintext value. Returns "aes256s:{salt_hex}:{iv_hex}:{ciphertext_hex}".
 */
export function encrypt(plaintext) {
  const salt = crypto.randomBytes(SALT_BYTES);
  const key = scryptKey(salt);
  const iv = crypto.randomBytes(16);
  const cipher = crypto.createCipheriv(ALGORITHM, key, iv);
  let ciphertext = cipher.update(plaintext, 'utf8', 'hex');
  ciphertext += cipher.final('hex');
  return `aes256s:${salt.toString('hex')}:${iv.toString('hex')}:${ciphertext}`;
}

/**
 * Check if AGENTO_ENCRYPTION_KEY is available.
 */
export function hasEncryptionKey() {
  return !!process.env.AGENTO_ENCRYPTION_KEY;
}
