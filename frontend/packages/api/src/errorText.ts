import { useCallback } from "react";
import { useLookup, type MessageParams } from "@agento/ui";
import { ApiError } from "./errors";

interface CodedBody { code?: unknown; reason?: unknown; params?: unknown }

const codedBody = (e: unknown): CodedBody | undefined =>
  e instanceof ApiError && typeof e.body === "object" && e.body !== null ? e.body as CodedBody : undefined;

/** The stable `code` of an API refusal; `undefined` for an answer with none, and for any other error. */
export function errorCode(e: unknown): string | undefined {
  const code = codedBody(e)?.code;
  return typeof code === "string" ? code : undefined;
}

/** The `reason` of an API refusal: a second, more exact code that a route adds when its `code`
 *  is a wide one that older clients know (`upload` with `reason: "UPLOAD_TOO_LARGE"`). */
function errorReason(e: unknown): string | undefined {
  const reason = codedBody(e)?.reason;
  return typeof reason === "string" ? reason : undefined;
}

function errorParams(e: unknown): MessageParams | undefined {
  const params = codedBody(e)?.params;
  if (typeof params !== "object" || params === null) return undefined;
  return Object.fromEntries(Object.entries(params).filter(([, v]) => typeof v === "string" || typeof v === "number"));
}

/** A failure that a component keeps in state: the error, and the key of the text for an error
 *  with no known code. The text is made at render (`errorText(f.error, f.fallbackKey)`), so it
 *  is in the language the panel shows now. With no `error` the text is that of `fallbackKey`. */
export interface Failure { error?: unknown; fallbackKey: string }

/** `errorText(e, fallbackKey)`: the text of an error for the user. A known reason reads
 *  `<namespace>:error.<REASON>`, then `panel:error.<REASON>`; then a known code reads
 *  `<namespace>:error.<CODE>`, then `panel:error.<CODE>`; an answer with neither reads
 *  `<namespace>:<fallbackKey>`, then `panel:<fallbackKey>`. The server's own text is never shown. */
export function useErrorText(namespace = "panel"): (e: unknown, fallbackKey: string) => string {
  const lookup = useLookup();
  return useCallback((e, fallbackKey) => {
    const params = errorParams(e);
    const textOf = (name: string | undefined) => (name === undefined
      ? undefined
      : lookup(namespace, `error.${name}`, params) ?? lookup("panel", `error.${name}`, params));
    return textOf(errorReason(e)) ?? textOf(errorCode(e))
      ?? lookup(namespace, fallbackKey) ?? lookup("panel", fallbackKey) ?? fallbackKey;
  }, [lookup, namespace]);
}
