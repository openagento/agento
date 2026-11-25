// The message catalogs `web` serves. Text only: a message is never rendered as HTML.
import { isMessage, type Messages } from "@agento/ui";
import { apiFetch, type ApiOptions } from "./apiFetch";
import { ApiError } from "./errors";
import { isLanguageTag, isRecord, shaped } from "./shape";

/** `fallback`: the English messages for the keys `messages` lacks. The login answer has none. */
export interface ServedCatalog { locale: string; version: string; messages: Messages; fallback?: Messages }

function isMessages(value: unknown): value is Messages {
  if (!isRecord(value)) return false;
  for (const namespace of Object.values(value)) {
    if (!isRecord(namespace)) return false;
    for (const message of Object.values(namespace)) if (!isMessage(message)) return false;
  }
  return true;
}

const isServedCatalog = (body: unknown): body is ServedCatalog =>
  isRecord(body) && isLanguageTag(body.locale) && typeof body.version === "string"
  && isMessages(body.messages) && (body.fallback === undefined || isMessages(body.fallback));

/** The one reader of a catalog answer. An answer that is not a catalog fails as a failed read does. */
const served = async (path: string, options: ApiOptions) => shaped(await apiFetch(path, options), isServedCatalog);

const pathOf = (tag: string) => `/api/panel/i18n/${encodeURIComponent(tag)}`;

/** `GET /api/panel/i18n/<tag>`: every message of one installed language. Needs a session. */
export const fetchCatalog = (tag: string, signal?: AbortSignal) => served(pathOf(tag), { signal });

const NOT_MODIFIED = 304;

/** The catalog of `tag` when the server's version is not `version`; `null` when it still is. */
export const fetchCatalogUnless = (tag: string, version: string, signal?: AbortSignal) =>
  served(pathOf(tag), { signal, ifNoneMatch: version }).catch((e: unknown) => {
    if (e instanceof ApiError && e.status === NOT_MODIFIED) return null;
    throw e;
  });

/** `GET /api/panel/bootstrap`: the sign-in screen's language and its texts. Needs no session. */
export const fetchBootstrap = (signal?: AbortSignal) => served("/api/panel/bootstrap", { signal, quiet401: true });
