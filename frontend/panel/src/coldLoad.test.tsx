import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { servedCatalog, sessionBody, sessionLocale, stubRoutes, teardown } from "./routes/admin/testing";

afterEach(() => { teardown(); window.history.pushState({}, "", "/"); });

describe("a cold load of the panel", () => {
  it("shows a signed-in user's first screen in the stored language, with no English first", async () => {
    const api = stubRoutes({
      "GET /api/session": () => sessionBody({ ui_locale: "pl", locale: sessionLocale("pl") }),
      "GET /api/panel/i18n/pl": () => servedCatalog("pl"),
    });
        const headings: string[] = [];
    const seen = new MutationObserver(() => {
      document.querySelectorAll("h1").forEach((h) => headings.push(h.textContent ?? ""));
    });
    seen.observe(document.body, { childList: true, subtree: true, characterData: true });

    const { App } = await import("./App");
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Witaj, ann" })).toBeInTheDocument();
    seen.disconnect();
    expect(new Set(headings)).toEqual(new Set(["Witaj, ann"]));
    expect(screen.getByRole("navigation", { name: "Główna" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "ann: Twój profil" })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("pl");
    expect(document.title).toBe("Witaj, ann · Agento");
    expect(api.paths().slice(0, 2)).toEqual(["GET /api/session", "GET /api/panel/i18n/pl"]);
  });
});
