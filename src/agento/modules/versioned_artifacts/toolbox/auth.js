import { randomBytes, scryptSync, createHash, timingSafeEqual } from 'node:crypto';

// Pure node:crypto, no framework code and no secret at rest — the SAME contract that
// lets `paths.js`/`errors.js` be imported by the serving container. The serving
// container reaches the store's password only as the scrypt HASH this file makes, never
// as the plaintext or the reversibly-encrypted DB copy: it holds no encryption key and
// no DB handle, so a one-way hash is the only credential it can be trusted with.

// The scrypt work factors, carried in the sidecar so a verification always uses the
// values its hash was made with — raising these later must not lock out an old sidecar.
const SCRYPT = Object.freeze({ N: 16384, r: 8, p: 1 });
const KEYLEN = 32;
// 128 * N * r bytes ~= 16 MiB at these factors; the default 32 MiB cap would still pass,
// but pinning it keeps a later N bump from failing with a cryptic ERR_CRYPTO_* instead.
const MAXMEM = 64 * 1024 * 1024;

/** A strong URL-safe password: 18 random bytes -> 24 base64url characters (~143 bits). */
export const generatePassword = () => randomBytes(18).toString('base64url');

/** The Basic-auth user when the operator names none — the artifact's own code, per the
 *  "leave both empty and the user defaults to the artifact name" rule. */
export const defaultAuthUser = (artifactCode) => artifactCode;

/** The credential the serving container verifies against. It carries the scrypt hash and
 *  its parameters, never the password. `salt` defaults to a fresh 16 bytes; passing one
 *  is for tests that need a fixed vector. */
export function hashSecret(password, saltHex = randomBytes(16).toString('hex')) {
  const derived = scryptSync(String(password), Buffer.from(saltHex, 'hex'), KEYLEN, { ...SCRYPT, maxmem: MAXMEM });
  return { algo: 'scrypt', ...SCRYPT, salt: saltHex, hash: derived.toString('hex') };
}

// Constant-time over VARIABLE-length inputs: `timingSafeEqual` throws when the two
// buffers differ in length, which would itself leak the answer. Comparing fixed-width
// digests instead makes the comparison, not the length, decide.
const constantEquals = (a, b) => timingSafeEqual(
  createHash('sha256').update(String(a)).digest(),
  createHash('sha256').update(String(b)).digest(),
);

/** Does `user:password` satisfy this sidecar? A malformed or non-scrypt sidecar always
 *  returns false, so a present-but-corrupt `.auth` fails CLOSED rather than serving open. */
export function verifyCredential(sidecar, user, password) {
  if (!sidecar || sidecar.algo !== 'scrypt'
    || typeof sidecar.user !== 'string' || typeof sidecar.salt !== 'string'
    || typeof sidecar.hash !== 'string') return false;
  const expected = Buffer.from(sidecar.hash, 'hex');
  if (expected.length === 0) return false;
  const derived = scryptSync(String(password), Buffer.from(sidecar.salt, 'hex'), expected.length,
    { N: sidecar.N ?? SCRYPT.N, r: sidecar.r ?? SCRYPT.r, p: sidecar.p ?? SCRYPT.p, maxmem: MAXMEM });
  // Both halves are always evaluated — no `&&` short-circuit — so a wrong user and a
  // wrong password cost the same.
  const userOk = constantEquals(user, sidecar.user);
  const passOk = timingSafeEqual(derived, expected);
  return userOk && passOk;
}
