// Test support for the admin screens: a fetch stub routed by "METHOD /path" (or "/path" for a GET),
// a signed-in session holding the given admin leaves (every leaf: the admin role), and the providers a screen needs. A `null` answer is a 204;
// an answer may be a Promise (a slow write) or a Response (an error status).
// `renderInLocale` shows a screen in one of the built-in languages, read from the shipped catalogs.
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { vi } from "vitest";
import { endSession, login, QueryClientProvider, queryClient } from "@agento/api";
import { AgentoUiProvider, I18nProvider, type Message, type Messages } from "@agento/ui";
import { cleanNotifications, Notifications } from "@mantine/notifications";

type Answer = unknown | ((body: unknown, url: URL) => unknown);

export const ALL_LEAVES = [
  "admin.dashboard", "admin.jobs", "admin.agents", "admin.credentials", "admin.credentials.edit", "admin.tools", "admin.tools.classify",
  "admin.skills", "admin.config", "admin.config.edit", "admin.users", "admin.users.create", "admin.users.edit",
  "admin.roles", "admin.roles.create", "admin.roles.edit", "admin.roles.delete",
  "versioned_artifacts", "versioned_artifacts.publish", "versioned_artifacts.delete",
];

/** The JSON body of a call; a multipart body (a staged file) has none. */
const parsed = (body: unknown) => (typeof body === "string" ? JSON.parse(body) : undefined);

/** What `GET /api/session` adds for the panel language; a test that needs none sends none. */
export interface SessionExtras {
  ui_locale?: string | null;
  locale?: { effective: string; instance: string; available: { tag: string; name: string }[]; version: string };
  display?: { date_format: string; timezone: string; date_format_explicit?: boolean } | null;
}

export const LANGUAGES = [{ tag: "en", name: "English" }, { tag: "pl", name: "Polski" }];
export const CATALOG_VERSION = "v1";
export const sessionLocale = (effective: string, instance = "en") =>
  ({ effective, instance, available: LANGUAGES, version: CATALOG_VERSION });

const CATALOG_FILES = {
  ...import.meta.glob<Record<string, Message>>("../../../../../src/agento/framework/i18n/*/*.json", { eager: true, import: "default" }),
  ...import.meta.glob<Record<string, Message>>("../../../../../src/agento/modules/*/i18n/*.json", { eager: true, import: "default" }),
};

/** Every shipped message of one language: the framework's `ui` and `panel`, and each core module's own. */
export function builtInMessages(tag: string): Messages {
  return Object.fromEntries(Object.entries(CATALOG_FILES).flatMap(([file, messages]) => {
    const framework = new RegExp(`/framework/i18n/([^/]+)/${tag}\\.json$`).exec(file);
    const module = new RegExp(`/modules/([^/]+)/i18n/${tag}\\.json$`).exec(file);
    const namespace = (framework ?? module)?.[1];
    return namespace ? [[namespace, messages]] : [];
  }));
}

export const servedBootstrap = (tag: string, version = CATALOG_VERSION) => ({
  locale: tag, version,
  messages: { panel: Object.fromEntries(Object.entries(builtInMessages(tag).panel).filter(([key]) => key.startsWith("login."))) },
});

/** A fetch stub routed by "METHOD /path"; a route with no answer is a 404. It signs nobody in. */
export function stubRoutes(answers: Record<string, () => unknown>) {
  const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const answer = answers[`${init?.method ?? "GET"} ${new URL(String(input), window.location.origin).pathname}`];
    const body = answer ? await answer() : new Response(JSON.stringify({ error: "not stubbed" }), { status: 404 });
    return body instanceof Response ? body : new Response(JSON.stringify(body), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    fetchMock,
    paths: () => fetchMock.mock.calls.map(([u, i]) => `${i?.method ?? "GET"} ${new URL(String(u), window.location.origin).pathname}`),
  };
}

export const sessionBody = (extras: SessionExtras = {}, acl: string[] = [], role = "user") => {
  const { ui_locale, ...rest } = extras;
  return {
    user: { id: 1, username: "ann", role, is_active: true, acl, ui_locale: ui_locale ?? null },
    csrf_token: "t", expires_at: "x", ...rest,
  };
};

export const servedCatalog = (tag: string, version = CATALOG_VERSION) =>
  ({ locale: tag, version, messages: builtInMessages(tag), fallback: {} });

const answered = (body: unknown) => () => new Response(JSON.stringify(body), { status: 200 });

/** Answers with status 200 that are not a served catalog. */
export const UNREADABLE_CATALOGS: [string, () => Response][] = [
  ["a body that is not JSON", () => new Response("<html>", { status: 200 })],
  ["null", answered(null)],
  ["an array", answered([])],
  ["no locale", answered({ version: "v1", messages: {} })],
  ["no version", answered({ locale: "pl", messages: {} })],
  ["no messages", answered({ locale: "pl", version: "v1" })],
  ["a locale that is a number", answered({ locale: 1, version: "v1", messages: {} })],
  ["messages that are a string", answered({ locale: "pl", version: "v1", messages: "panel" })],
  ["messages that are an array", answered({ locale: "pl", version: "v1", messages: [] })],
  ["a namespace that is a string", answered({ locale: "pl", version: "v1", messages: { panel: "x" } })],
  ["a namespace that is null", answered({ locale: "pl", version: "v1", messages: { panel: null } })],
  ["a fallback that is a string", answered({ locale: "pl", version: "v1", messages: {}, fallback: "x" })],
  ...([
    ["a plural form that is a number", { other: "{count} jobs", few: 7 }],
    ["a plural category that no language has", { other: "{count} jobs", several: "{count} jobs" }],
    ["a plural with no `other`", { one: "{count} job" }],
    ["a message that is a number", 7],
    ["a message that is an array", ["x"]],
    ["a message that is null", null],
  ] as const).flatMap(([name, message]): [string, () => Response][] => [
    [name, answered({ locale: "pl", version: "v1", messages: { panel: { "login.title": message } } })],
    [`${name}, in the fallback`, answered({ locale: "pl", version: "v1", messages: {}, fallback: { panel: { "login.title": message } } })],
  ]),
  ...["x_y", "PL", "pl-pl", "polski", ""].map((tag): [string, () => Response] =>
    [`a locale that is not a language tag (${JSON.stringify(tag)})`, answered({ locale: tag, version: "v1", messages: {} })]),
];

/** A 200 whose headers arrived and whose body never ends: the read of the body fails when the
 *  request is aborted, as in a browser. */
export const stalledBody = (init?: RequestInit) => ({
  ok: true, status: 200, statusText: "", headers: new Headers(),
  json: () => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }),
}) as unknown as Response;

export async function stubApi(answers: Record<string, Answer>, acl: string[] = ALL_LEAVES, session: SessionExtras = {}) {
  const role = acl === ALL_LEAVES ? "admin" : "user";
  const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const url = new URL(String(input), window.location.origin);
    const method = init?.method ?? "GET";
    if (url.pathname === "/api/session") {
      return new Response(JSON.stringify(sessionBody(session, acl, role)));
    }
    const key = [`${method} ${url.pathname}${url.search}`, `${method} ${url.pathname}`, method === "GET" ? url.pathname : ""]
      .find((k) => k && k in answers);
    if (!key) return new Response(JSON.stringify({ error: "not stubbed" }), { status: 404 });
    const a = answers[key];
    const body = await (typeof a === "function" ? a(parsed(init?.body), url) : a);
    if (body instanceof Response) return body;
    return body === null ? new Response(null, { status: 204 }) : new Response(JSON.stringify(body), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  await login("ann", "pw");
  return {
    fetchMock,
    /** Every call to `METHOD path` (path without the query), with its parsed JSON body and its headers. */
    calls: (method: string, path: string) => fetchMock.mock.calls
      .filter(([u, i]) => (i?.method ?? "GET") === method && new URL(String(u), window.location.origin).pathname === path)
      .map(([u, i]) => ({
        url: new URL(String(u), window.location.origin), body: parsed(i?.body), headers: (i?.headers ?? {}) as Record<string, string>,
      })),
  };
}

// cleanNotifications: the store is global, and Mantine queues beyond its limit — a test that
// leaves notifications behind would hide the next test's own.
export function teardown() { endSession(); vi.unstubAllGlobals(); queryClient.clear(); cleanNotifications(); }

/** The providers a screen needs; also a `wrapper` for `render`/`renderHook` (it survives `rerender`). */
export const Providers = ({ path = "/", children }: { path?: string; children: ReactNode }) => (
  <AgentoUiProvider env="test">
    <Notifications />
    <QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[path]}>{children}</MemoryRouter></QueryClientProvider>
  </AgentoUiProvider>
);

export const renderAt = (path: string, ui: ReactNode) => render(<Providers path={path}>{ui}</Providers>);

/** `ui` at `path`, shown in `locale` with the shipped messages of that language. `extra` adds
 *  messages a test needs that no built-in catalog holds (a module's derived config key). */
export const renderInLocale = (locale: string, path: string, ui: ReactNode, extra: Messages = {}) => render(
  <I18nProvider locale={locale} messages={{ ...builtInMessages(locale), ...extra }}><Providers path={path}>{ui}</Providers></I18nProvider>,
);
