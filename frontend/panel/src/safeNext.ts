/** A `next` target is a same-origin path; anything else (`//x`, `https://x`, `\x`) is `/`. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) return "/";
  try {
    const u = new URL(next, window.location.origin);
    return u.origin === window.location.origin && !u.pathname.startsWith("/login") ? u.pathname + u.search : "/";
  } catch {
    return "/";
  }
}
