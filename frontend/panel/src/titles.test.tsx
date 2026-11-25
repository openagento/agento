import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, Link, Route, RouterProvider, Routes, type RouteObject } from "react-router";
import { afterAll, describe, expect, it } from "vitest";
import { PanelModulesProvider, QueryClientProvider, queryClient } from "@agento/api";
import { AgentoUiProvider, PageHeader } from "@agento/ui";
import { ModuleRoute } from "./ModuleRoute";
import { MODULES } from "./generated/registry";
import { ALL_LEAVES, renderInLocale, sessionBody, sessionLocale, stubRoutes, teardown } from "./routes/admin/testing";

const EARLIER = "The screen before · Agento";
const session = () => sessionBody({ ui_locale: "en", locale: sessionLocale("en") }, ALL_LEAVES, "admin");
const probesAnswer = Object.fromEntries(MODULES.map((m) => [`GET ${m.availability.probe}`, () => []]));
const withValues = (path: string) => path.replace(/:threadId/g, "5").replace(/:[A-Za-z]+/g, "x1");

/** The router asks for the session when its module loads, so the answers come first. */
async function panelRoutes(answers: Record<string, () => unknown>): Promise<RouteObject[]> {
  stubRoutes({ "GET /api/session": session, ...answers });
  return (await import("./router")).routes;
}

async function titleAt(routes: RouteObject[], path: string): Promise<string> {
  queryClient.clear();
  document.title = EARLIER;
  render(
    <AgentoUiProvider env="test">
      <QueryClientProvider client={queryClient}>
        <PanelModulesProvider modules={MODULES}>
          <RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />
        </PanelModulesProvider>
      </QueryClientProvider>
    </AgentoUiProvider>,
  );
  await waitFor(() => expect(document.title).not.toBe(EARLIER)).catch(() => undefined);
  cleanup();
  return document.title;
}

afterAll(teardown);

describe("the tab title", () => {
  it("is set by every routed screen, also when each read of the screen fails", async () => {
    const routes = await panelRoutes(probesAnswer);
    const screens = (routes.find((r) => r.path === "/")!.children as RouteObject[])
      .map((r) => (r.index ? "/" : r.path === "*" ? "/no-such-screen" : `/${withValues(r.path!)}`));
    expect(screens.length).toBeGreaterThan(MODULES.length);
    const untitled: string[] = [];
    for (const path of screens) {
      const title = await titleAt(routes, path);
      if (title === EARLIER || title === "Sign in · Agento" || !title.endsWith(" · Agento")) untitled.push(`${path}: ${title}`);
    }
    expect(untitled).toEqual([]);
  }, 60_000);

  it("is set by the screen of a module that is off", async () => {
    const routes = await panelRoutes({});
    for (const m of MODULES) for (const r of m.routes) {
      expect(await titleAt(routes, withValues(r.path)), r.path).toBe("Not available · Agento");
    }
  });

  it.each([["en", "Not available · Agento"], ["pl", "Niedostępne · Agento"]])(
    "changes from a titled screen to a module that is off, in %s", async (tag, title) => {
      stubRoutes({});
      queryClient.clear();
      renderInLocale(tag, "/", (
        <Routes>
          <Route path="/" element={<><PageHeader title="A titled screen" /><Link to="/module">module</Link></>} />
          <Route path="/module" element={<ModuleRoute module={MODULES[0]} Screen={() => null} />} />
        </Routes>
      ));
      expect(document.title).toBe("A titled screen · Agento");
      fireEvent.click(screen.getByRole("link", { name: "module" }));
      await waitFor(() => expect(document.title).toBe(title));
    });
});
