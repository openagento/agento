import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Home } from "../Home";
import { Dashboard } from "./Dashboard";
import { renderAt, renderInLocale, stubApi, teardown } from "./testing";

const DASH = {
  db_connected: true, running_jobs: 4, version: "0.17.0", python_version: "3.12.1", module_count: 23,
  recent_jobs: [{ id: 9, type: "cron", status: "RUNNING", reference_id: "AG-9", agent_view_code: "av",
    created_at: "2026-10-02T10:00:00Z", finished_at: null }],
  credentials: [{ id: 1, scope: "claude", label: "main", status: "error", enabled: true,
    error_source: "auto", used_at: null, expires_at: null }],
  agent_views: [{ id: 3, code: "dev", label: "Developer", workspace_id: 1 }],
};
afterEach(teardown);

describe("Home", () => {
  it("is the dashboard for an admin", async () => {
    await stubApi({ "/api/admin/dashboard": DASH });
    renderAt("/", <Home />);
    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("Python 3.12.1")).toBeInTheDocument();
    expect(screen.getByText("AG-9")).toBeInTheDocument();
    expect(screen.getByText("Developer")).toBeInTheDocument();
    expect(screen.getByText("error")).toBeInTheDocument();
  });

  it("is a greeting for a user, with no admin request", async () => {
    const api = await stubApi({}, []);
    renderAt("/", <Home />);
    expect(await screen.findByText("Hello, ann")).toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/dashboard")).toEqual([]);
  });
});

describe("Dashboard", () => {
  it.each([
    ["en", "Dashboard", "Connected", "Recent jobs", "Reference", "Dashboard · Agento"],
    ["pl", "Pulpit", "Połączono", "Ostatnie zadania", "Odniesienie", "Pulpit · Agento"],
  ])("shows the screen in %s", async (tag, title, database, section, column, tab) => {
    await stubApi({ "/api/admin/dashboard": DASH });
    renderInLocale(tag, "/", <Dashboard />);
    expect(await screen.findByText(database)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: section })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: column })).toBeInTheDocument();
    expect(document.title).toBe(tab);
  });

  it("shows a disabled credential in Polish", async () => {
    await stubApi({ "/api/admin/dashboard": { ...DASH, credentials: [{ ...DASH.credentials[0], enabled: false }] } });
    renderInLocale("pl", "/", <Dashboard />);
    expect(await screen.findByText("wyłączone")).toBeInTheDocument();
  });

  it.each([
    ["a known code", { error: "server words", code: "FORBIDDEN" }, "Nie masz uprawnień do tej operacji."],
    ["no code", { error: "server words" }, "Nie udało się wczytać pulpitu."],
  ])("a read refused with %s shows Polish text, never the server's", async (_name, body, text) => {
    await stubApi({ "/api/admin/dashboard": () => new Response(JSON.stringify(body), { status: 403 }) });
    renderInLocale("pl", "/", <Dashboard />);
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByText(/server words/)).toBeNull();
  });
});
