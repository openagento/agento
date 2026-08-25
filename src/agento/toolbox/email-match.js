// Shared address-allow-list matcher. It lives at the toolbox (framework) level, not inside a
// module, because both `core` (outbound `email_send`) and `outlook` (inbound gate + reply
// recipients) need it and CLAUDE.md tells modules to prefer framework code over an inter-module
// dependency — an import from `core` would also be undeclared in outlook's `sequence`.
//
// Semantics, kept in lockstep with the Python twin `_matches_allowed`
// (src/agento/modules/outlook/src/channel.py): anchored, case-insensitive; the glob `*` expands to
// `[^@]*`, so a wildcard matches a local part but NEVER crosses the `@`; every OTHER regex
// metachar in the literal segments is escaped, so `a?b@x.com` matches literally rather than as a
// `?` quantifier (escaping the fail-OPEN direction). An EMPTY list matches nothing — blocks all.

export function matchesWhitelist(email, whitelist) {
  const addr = (email || '').trim().toLowerCase();
  // FAIL-CLOSED: a message with no `from` address is not "everyone" — it is nobody. Without this
  // a pattern of `*` would admit it, and the old unguarded `email.toLowerCase()` threw outright.
  if (!addr) return false;
  if (!Array.isArray(whitelist)) return false;
  return whitelist.some((pattern) => {
    const re =
      '^' +
      String(pattern || '')
        .toLowerCase()
        .split('*')
        .map((seg) => seg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
        .join('[^@]*') +
      '$';
    return new RegExp(re).test(addr);
  });
}
