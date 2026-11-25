import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { hub } from "@agento/api";
import { servedCatalog, sessionBody, sessionLocale, stubRoutes, teardown } from "./routes/admin/testing";

afterEach(() => { teardown(); vi.restoreAllMocks(); window.history.pushState({}, "", "/"); });

const THREAD = {
  id: 5, agent_view_id: 1, title: null, status: "active", created_at: null, updated_at: null, channel: "panel",
  external_ref: null, last_activity_at: null, live: false, runs: [], jobs: [],
};

const ENGLISH_REFUSAL = "The file is empty.";
const POLISH_REFUSAL = "Plik jest pusty.";

describe("a language change from the header, with a conversation open", () => {
  it("keeps the route, the draft and the one stream, and shows the conversation and a failed attachment in Polish", async () => {
    const api = stubRoutes({
      "GET /api/session": () => sessionBody({ ui_locale: null, locale: sessionLocale("en") }),
      "GET /api/conversation/threads": () => [{ ...THREAD, id: 6, title: "Yesterday's notes", last_activity_at: new Date().toISOString() }],
      "GET /api/agent-views": () => [],
      "GET /api/conversation/threads/5": () => THREAD,
      "GET /api/conversation/threads/5/timeline": () => ({ events: [], has_older: false, newest_id: null }),
      "GET /api/conversation/threads/5/messages": () => [],
      "GET /api/panel/i18n/pl": () => servedCatalog("pl"),
      "PATCH /api/profile/locale": () => ({ ui_locale: "pl", locale: sessionLocale("pl") }),
      "POST /api/conversation/threads/5/uploads": () => new Response(JSON.stringify({ error: "server words", code: "UPLOAD_EMPTY" }), { status: 400 }),
    });
    const open = vi.spyOn(hub, "open").mockImplementation(() => undefined);
    const close = vi.spyOn(hub, "close");
    window.history.pushState({}, "", "/conversations/5");
    const { App } = await import("./App");
    render(<App />);

    const draft = await screen.findByPlaceholderText("Ask the agent…");
    const threadRow = await screen.findByRole("button", { name: "Yesterday's notes", hidden: true });
    expect(screen.getByText("Today")).toBeInTheDocument();
    fireEvent.change(draft, { target: { value: "half a sentence" } });
    fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [new File(["x"], "report.pdf")] } });
    const refusedInEnglish = await screen.findByText(ENGLISH_REFUSAL);
    fireEvent.click(screen.getByRole("button", { name: "ann: your profile" }));
    const dialog = await screen.findByRole("dialog", { name: "Profile" });
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Language" }));
    fireEvent.click(await screen.findByRole("option", { name: "Polski", hidden: true }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("dialog", { name: "Profil" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/conversations/5");
    expect(screen.getByPlaceholderText("Zapytaj agenta…")).toBe(draft);
    expect(draft).toHaveValue("half a sentence");
    expect(screen.getByText("report.pdf")).toBeInTheDocument();
    expect(screen.getByText(POLISH_REFUSAL)).toBe(refusedInEnglish);
    expect(screen.queryByText(ENGLISH_REFUSAL)).toBeNull();
    expect(screen.getByText("Dzisiaj")).toBeInTheDocument();
    expect(threadRow.isConnected).toBe(true);
    expect(screen.getByRole("button", { name: "Yesterday's notes", hidden: true })).toBe(threadRow);
    expect(screen.getByRole("heading", { name: "Rozmowa 5", hidden: true })).toBeInTheDocument();
    expect(open).toHaveBeenCalledTimes(1);
    expect(close).not.toHaveBeenCalled();
    expect(document.title).toBe("Rozmowy · Agento");
    expect(api.paths().filter((p) => p === "PATCH /api/profile/locale")).toHaveLength(1);
  });
});
