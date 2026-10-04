// Test support for the admin screens: a fetch stub routed by "METHOD /path" (or "/path" for a GET),
// a signed-in session with the given role, and the providers a screen needs. A `null` answer is a 204;
// an answer may be a Promise (a slow write) or a Response (an error status).
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { vi } from "vitest";
import { endSession, login, QueryClientProvider, queryClient } from "@agento/api";
import { AgentoUiProvider } from "@agento/ui";
import { Notifications } from "@mantine/notifications";

type Answer = unknown | ((body: unknown, url: URL) => unknown);

export async function stubApi(answers: Record<string, Answer>, role: "admin" | "user" = "admin") {
  const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const url = new URL(String(input), window.location.origin);
    const method = init?.method ?? "GET";
    if (url.pathname === "/api/session") {
      return new Response(JSON.stringify({ user: { id: 1, username: "ann", role, is_active: true }, csrf_token: "t", expires_at: "x" }));
    }
    const key = [`${method} ${url.pathname}${url.search}`, `${method} ${url.pathname}`, method === "GET" ? url.pathname : ""]
      .find((k) => k && k in answers);
    if (!key) return new Response(JSON.stringify({ error: "not stubbed" }), { status: 404 });
    const a = answers[key];
    const body = await (typeof a === "function" ? a(init?.body ? JSON.parse(String(init.body)) : undefined, url) : a);
    if (body instanceof Response) return body;
    return body === null ? new Response(null, { status: 204 }) : new Response(JSON.stringify(body), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  await login("ann", "pw");
  return {
    fetchMock,
    /** Every call to `METHOD path` (path without the query), with its parsed JSON body. */
    calls: (method: string, path: string) => fetchMock.mock.calls
      .filter(([u, i]) => (i?.method ?? "GET") === method && new URL(String(u), window.location.origin).pathname === path)
      .map(([u, i]) => ({ url: new URL(String(u), window.location.origin), body: i?.body ? JSON.parse(String(i.body)) : undefined })),
  };
}

export function teardown() { endSession(); vi.unstubAllGlobals(); queryClient.clear(); }

export const renderAt = (path: string, ui: ReactNode) => render(
  <AgentoUiProvider env="test">
    <Notifications />
    <QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter></QueryClientProvider>
  </AgentoUiProvider>,
);
