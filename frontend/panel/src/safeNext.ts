/** A `next` target is a same-origin path; anything else (`//x`, `https://x`, `\x`) is `/`. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) return "/";
  try {
    const u = new URL(next, window.location.origin);
    // Checked AFTER parsing: dot-segments turn `/.//evil.example` into the path `//evil.example`.
    const path = u.pathname + u.search;
    return u.origin === window.location.origin && !path.startsWith("//") && !u.pathname.startsWith("/login") ? path : "/";
  } catch {
    return "/";
  }
}
