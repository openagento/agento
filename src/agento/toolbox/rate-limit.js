// Rate limits for every route the toolbox serves (RULES.md SEC-12). Mounted with app.use()
// before any route, so a route added later is limited with no opt-in.
//
// Two limits, in this order:
// 1. Auth failures (401/403) per client address. Every sandbox run shares one address, so this
//    counts only failures: it bounds a random-token flood (each random token would get a fresh
//    bucket in limit 2) without throttling authorized traffic. A held `GET /sse` counts as one
//    pending request until its stream closes, which the ceiling allows for.
// 2. All requests per capability: the key is the token's SHA-256, never the raw token (SEC-6).
//    A request with no token is keyed by address.
//
// The stores are process-wide by design (TBX-1): buckets expire with their window.
import { rateLimit, ipKeyGenerator } from 'express-rate-limit';
import { extractToken, tokenHash } from './capability.js';

export const WINDOW_MS = 60_000;
export const AUTH_FAILURES_PER_ADDRESS = 60;
export const REQUESTS_PER_CAPABILITY = 600;

const addressKey = (req) => `addr:${ipKeyGenerator(req.ip || '')}`;

export function createRateLimits({
  windowMs = WINDOW_MS,
  authFailuresPerAddress = AUTH_FAILURES_PER_ADDRESS,
  requestsPerCapability = REQUESTS_PER_CAPABILITY,
} = {}) {
  const common = {
    windowMs,
    standardHeaders: 'draft-8',
    legacyHeaders: false,
    message: { error: 'rate limited' },
  };
  const authFailures = rateLimit({
    ...common,
    limit: authFailuresPerAddress,
    keyGenerator: addressKey,
    skipSuccessfulRequests: true,
    requestWasSuccessful: (req, res) => res.statusCode !== 401 && res.statusCode !== 403,
  });
  const perCapability = rateLimit({
    ...common,
    limit: requestsPerCapability,
    keyGenerator: (req) => {
      const token = extractToken(req);
      return token ? `cap:${tokenHash(token)}` : addressKey(req);
    },
  });
  return [authFailures, perCapability];
}
