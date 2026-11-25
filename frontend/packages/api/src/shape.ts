// A 2xx answer is data from outside: `apiFetch` gives it as `unknown` to the readers below, and
// each reader gives it a type only through `shaped`.
export const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** The server's `TAG_RE`: the only form of a language tag that it installs, stores or sends. */
export const LANGUAGE_TAG = /^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$/;
export const isLanguageTag = (value: unknown): value is string => typeof value === "string" && LANGUAGE_TAG.test(value);

/** `body` when it has the shape; else the error of a read that failed, as when the network is down. */
export function shaped<T>(body: unknown, hasShape: (body: unknown) => body is T): T {
  if (hasShape(body)) return body;
  throw new TypeError("the answer is not readable");
}
