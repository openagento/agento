// The sign-in screen has its own texts. A signed-in user who opens /login and goes back must
// find the account's catalog as it was: the route writes nothing into the session's store.
import { act, render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it } from "vitest";
import { PanelModulesProvider, QueryClientProvider, queryClient } from "@agento/api";
import { AgentoUiProvider } from "@agento/ui";
import { getCatalog } from "./catalogStore";
import { SessionDisplayFormat } from "./DisplayFormat";
import { SessionI18n } from "./I18n";
import { CATALOG_VERSION, servedBootstrap, servedCatalog, sessionBody, sessionLocale, stubRoutes, teardown } from "./routes/admin/testing";

afterEach(() => { teardown(); });

describe("a signed-in user opens /login and goes back", () => {
  it("the protected screen and the store keep the account's Polish catalog, with the sign-in texts read or not", async () => {
    let bootstrap: () => unknown = () => servedBootstrap("en");
    const api = stubRoutes({
      "GET /api/session": () => sessionBody({ ui_locale: "pl", locale: sessionLocale("pl") }),
      "GET /api/panel/i18n/pl": () => servedCatalog("pl"),
      "GET /api/panel/bootstrap": () => bootstrap(),
    });
    // The module starts the session read when it loads, so it loads after the stub.
    const { routes } = await import("./router");
    const router = createMemoryRouter(routes, { initialEntries: ["/"] });
    render(
      <AgentoUiProvider env="test">
        <QueryClientProvider client={queryClient}>
          <SessionI18n>
            <SessionDisplayFormat>
              <PanelModulesProvider modules={[]}><RouterProvider router={router} /></PanelModulesProvider>
            </SessionDisplayFormat>
          </SessionI18n>
        </QueryClientProvider>
      </AgentoUiProvider>,
    );
    expect(await screen.findByRole("heading", { name: "Witaj, ann" })).toBeInTheDocument();
    const account = getCatalog();
    expect(account).toMatchObject({ locale: "pl", version: CATALOG_VERSION });

    const toLoginAndBack = async () => {
      await act(() => router.navigate("/login"));
      expect(await screen.findByRole("group", { name: "Sign in to Agento" })).toBeInTheDocument();
      expect(document.documentElement.lang).toBe("en");
      expect(document.title).toBe("Sign in · Agento");
      expect(getCatalog()).toBe(account);

      await act(() => router.navigate(-1));
      expect(await screen.findByRole("heading", { name: "Witaj, ann" })).toBeInTheDocument();
      expect(screen.getByRole("navigation", { name: "Główna" })).toBeInTheDocument();
      expect(document.documentElement.lang).toBe("pl");
      expect(getCatalog()).toBe(account);
    };
    await toLoginAndBack();
    bootstrap = () => new Response(JSON.stringify({ error: "boom" }), { status: 500 });
    await toLoginAndBack();
    expect(api.paths().filter((p) => p === "GET /api/panel/i18n/pl")).toHaveLength(1);
  });
});
