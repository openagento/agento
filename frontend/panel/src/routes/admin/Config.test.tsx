import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Config } from "./Config";
import { renderAt, renderInLocale, stubApi, teardown } from "./testing";

const field = (over: object) => ({
  path: "jira/url", label: "URL", description: "", type: "string", source: "db", editable: true,
  allowed_scopes: ["default"], options: null, tester: "", max_length: null, secret: false, is_set: true, value: "https://x", ...over,
});
const FIELDS = [
  field({}),
  // A server bug that sent a secret's value must still render nothing of it.
  field({ path: "jira/token", label: "Token", type: "obscure", secret: true, is_set: true, value: "hunter2", tester: "http" }),
  field({ path: "jira/tools/jira_search/limit", label: "Limit", type: "integer", source: "json", value: "10" }),
];
const base = {
  "/api/admin/scopes": { workspaces: [], agent_views: [{ id: 3, code: "av", label: "AV", workspace_id: 1, is_active: true }] },
  "/api/admin/config/modules": [{ name: "jira", tools: ["jira_search"] }, { name: "smtp", tools: [] }],
  "/api/admin/config": FIELDS,
};
const editOf = (label: string, edit = "Edit") => {
  const row = screen.getByText(label).closest("tr")!;
  fireEvent.click(within(row).getByRole("button", { name: edit }));
};
afterEach(teardown);

describe("Config", () => {
  it("keeps module, tool and scope in the URL, so a reload keeps them", async () => {
    const api = await stubApi(base);
    renderAt("/admin/config?module=jira&tool=jira_search&scope=agent_view&scope_id=3", <Config />);
    expect(await screen.findByText("Limit")).toBeInTheDocument();
    expect(screen.queryByText("URL")).toBeNull();
    const q = api.calls("GET", "/api/admin/config")[0].url.searchParams;
    expect([q.get("module"), q.get("scope"), q.get("scope_id")]).toEqual(["jira", "agent_view", "3"]);
    expect(screen.queryByText(/ENV overrides/)).toBeNull();
  });

  it("lists a module by its title and keeps its key in the URL", async () => {
    const api = await stubApi({ ...base, "/api/admin/config/modules": [{ name: "jira", tools: [] }, { name: "agent_view", tools: [] }] });
    renderAt("/admin/config", <Config />);
    fireEvent.click(await screen.findByText("Agent View"));
    await vi.waitFor(() => expect(api.calls("GET", "/api/admin/config").map((c) => c.url.searchParams.get("module")))
      .toContain("agent_view"));
    expect(screen.queryByText("agent_view")).toBeNull();
  });

  it("a secret row shows Set, never the value, and its editor is disabled", async () => {
    await stubApi(base);
    renderAt("/admin/config", <Config />);
    expect(await screen.findByText("Token")).toBeInTheDocument();
    expect(screen.queryByText(/hunter2/)).toBeNull();
    expect(screen.getByText("Set")).toBeInTheDocument();
    editOf("Token");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/bin\/agento config:set\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: "Value" })).toBeDisabled();
    expect(within(dialog).getByRole("textbox", { name: "Value" })).toHaveValue("");
    expect(within(dialog).getByRole("textbox", { name: "Value" })).toHaveAttribute("placeholder", "****");
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.queryByDisplayValue(/hunter2/)).toBeNull();
  });

  it("a secret with no value shows no mask", async () => {
    await stubApi({ ...base, "/api/admin/config": [field({ path: "jira/token", label: "Token", type: "obscure", secret: true, is_set: false, value: null })] });
    renderAt("/admin/config", <Config />);
    expect(await screen.findByText("Not set")).toBeInTheDocument();
    editOf("Token");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("textbox", { name: "Value" })).not.toHaveAttribute("placeholder");
  });

  it("Save PUTs the value at the scope and names the reset dependents", async () => {
    const api = await stubApi({ ...base, "PUT /api/admin/config": { path: "jira/url", reset: ["jira/project"] } });
    renderAt("/admin/config?scope=agent_view&scope_id=3", <Config />);
    await screen.findByText("URL");
    editOf("URL");
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Value" }), { target: { value: "https://y" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(api.calls("PUT", "/api/admin/config")).toHaveLength(1));
    expect(api.calls("PUT", "/api/admin/config")[0].body).toEqual({ path: "jira/url", value: "https://y", scope: "agent_view", scope_id: 3 });
    expect(await screen.findByText("Also reset: jira/project")).toBeInTheDocument();
  });

  it("a select is searchable, so a long list (time zones) can be typed into", async () => {
    const zones = [{ value: "browser", label: "Browser time zone" }, { value: "Europe/Warsaw", label: "Europe/Warsaw" }];
    await stubApi({ ...base, "/api/admin/config": [field({ path: "admin/locale/timezone", label: "Time zone", type: "select",
      options: zones, value: "browser" })] });
    renderAt("/admin/config", <Config />);
    await screen.findByText("Time zone");
    editOf("Time zone");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("combobox", { name: "Value" })).not.toHaveAttribute("readonly");
  });

  it("shows the fixed option of a time zone list in Polish, and an IANA name as it is", async () => {
    const zones = [{ value: "browser", label: "Browser time zone", label_key: "browser_timezone" }, { value: "Europe/Warsaw", label: "Europe/Warsaw" }];
    await stubApi({ ...base, "/api/admin/config": [field({ path: "admin/locale/timezone", label: "Time zone", type: "select",
      options: zones, value: "browser" })] });
    renderInLocale("pl", "/admin/config", <Config />);
    await screen.findByText("Strefa czasowa");
    fireEvent.click(screen.getByRole("button", { name: "Edytuj" }));
    const dialog = await screen.findByRole("dialog");
    const select = within(dialog).getByRole("combobox", { name: "Wartość" });
    expect(select).toHaveValue("Strefa czasowa przeglądarki");
    fireEvent.click(select);
    expect(await screen.findByRole("option", { name: "Europe/Warsaw", hidden: true })).toBeInTheDocument();
    expect(screen.queryByText("Browser time zone")).toBeNull();
  });

  it("Remove override asks first, then DELETEs the path at the scope", async () => {
    const api = await stubApi({ ...base, "DELETE /api/admin/config": null });
    renderAt("/admin/config", <Config />);
    await screen.findByText("URL");
    editOf("URL");
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Remove override" }));
    const confirm = await screen.findByRole("dialog", { name: "Remove this override?" });
    expect(api.calls("DELETE", "/api/admin/config")).toEqual([]);
    fireEvent.click(within(confirm).getByRole("button", { name: "Remove override" }));
    await vi.waitFor(() => expect(api.calls("DELETE", "/api/admin/config")).toHaveLength(1));
    expect(api.calls("DELETE", "/api/admin/config")[0].body).toEqual({ path: "jira/url", scope: "default", scope_id: 0 });
    expect(await screen.findByText("Override of jira/url removed.")).toBeInTheDocument();
  });

  it("Test POSTs the path and keeps the result as Last", async () => {
    const api = await stubApi({ ...base, "POST /api/admin/config/test": { status: "fail", code: "auth", message: "401 from Jira" } });
    renderAt("/admin/config", <Config />);
    await screen.findByText("Token");
    editOf("Token");
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Test" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/config/test")).toHaveLength(1));
    expect(api.calls("POST", "/api/admin/config/test")[0].body).toEqual({ path: "jira/token", scope: "default", scope_id: 0 });
    expect(await within(screen.getByRole("dialog")).findByText(/Last test:/)).toBeInTheDocument();
  });

  it.each([
    ["en", "TOOLBOX_UNREACHABLE", "Edit", "Test", "The toolbox is not reachable."],
    ["pl", "TOOLBOX_UNREACHABLE", "Edytuj", "Testuj", "Toolbox jest nieosiągalny."],
    ["pl", "A_CODE_NO_CATALOG_HOLDS", "Edytuj", "Testuj", "Test nie mógł sprawdzić tej wartości. Szczegóły pokaże polecenie bin/agento config:test."],
  ])("an error result reads its text by code in %s (%s), never the server's words", async (tag, code, edit, run, text) => {
    await stubApi({ ...base, "POST /api/admin/config/test": { status: "error", code, message: "server words" } });
    renderInLocale(tag, "/admin/config", <Config />);
    await screen.findByText("Token");
    editOf("Token", edit);
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: run }));
    expect((await screen.findAllByText(text, { exact: false })).length).toBeGreaterThan(0);
    expect(screen.queryByText(/server words/)).not.toBeInTheDocument();
  });

  it("a local tester (the model check) has a Test button and its result shows like any other", async () => {
    const model = field({ path: "agent_view/model", label: "Model", value: "gpt-5", tester: "local" });
    const api = await stubApi({ ...base, "/api/admin/config": [model],
      "POST /api/admin/config/test": { status: "ok", code: "MODEL_OK", message: "model gpt-5 is in the account's model list" } });
    renderAt("/admin/config", <Config />);
    await screen.findByText("Model");
    editOf("Model");
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Test" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/config/test")).toHaveLength(1));
    expect(api.calls("POST", "/api/admin/config/test")[0].body).toEqual({ path: "agent_view/model", scope: "default", scope_id: 0 });
    expect(await within(screen.getByRole("dialog")).findByText(/in the account's model list/)).toBeInTheDocument();
  });

  it.each([
    ["en", "Config", "Modules", "Field", "Edit", "Config · Agento"],
    ["pl", "Konfiguracja", "Moduły", "Pole", "Edytuj", "Konfiguracja · Agento"],
  ])("shows the screen in %s and names the tab", async (tag, title, modules, column, edit, tab) => {
    await stubApi(base);
    renderInLocale(tag, "/admin/config", <Config />);
    expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
    expect(await screen.findByRole("navigation", { name: modules })).toBeInTheDocument();
    expect(await screen.findByRole("columnheader", { name: column })).toBeInTheDocument();
    expect(within((await screen.findByText("URL")).closest("tr")!).getByRole("button", { name: edit })).toBeInTheDocument();
    expect(document.title).toBe(tab);
  });

  it("a field, its options and a module show their catalog text, and the manifest text where the catalog has none", async () => {
    const options = [{ value: "cloud", label: "Cloud" }, { value: "server", label: "Server" }];
    await stubApi({ ...base, "/api/admin/config": [
      field({ description: "The site address" }),
      field({ path: "jira/tools/jira_search/limit", label: "Limit", description: "Rows per search" }),
      field({ path: "jira/kind", label: "Kind", type: "select", options, value: "cloud" }),
    ] });
    renderInLocale("pl", "/admin/config", <Config />, { jira: {
      "module.title": "Dżira",
      "config.url.label": "Adres", "config.url.description": "Adres witryny",
      "config.kind.label": "Rodzaj", "config.kind.option.cloud": "Chmura",
    } });
    expect(await screen.findByText("Adres")).toBeInTheDocument();
    expect(screen.getByText("Dżira")).toBeInTheDocument();
    expect(screen.getByText("Smtp")).toBeInTheDocument();
    expect(screen.queryByText("URL")).toBeNull();
    expect(screen.getByText("Limit")).toBeInTheDocument();

    editOf("Adres", "Edytuj");
    expect(within(await screen.findByRole("dialog")).getByText("Adres witryny")).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Anuluj" }));

    editOf("Rodzaj", "Edytuj");
    const dialog = await screen.findByRole("dialog", { name: /Rodzaj/ });
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Wartość" }));
    expect(await screen.findByRole("option", { name: "Chmura", hidden: true })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Server", hidden: true })).toBeInTheDocument();
  });

  it.each([
    ["a known code", { error: "server words", code: "FORBIDDEN" }, "Zapisano 0 z 1. jira/url: Nie masz uprawnień do tej operacji."],
    ["a code the panel does not know", { error: "server words", code: "BRAND_NEW_CODE" }, "Zapisano 0 z 1. jira/url: Żądanie nie powiodło się."],
    ["no code", { error: "server words" }, "Zapisano 0 z 1. jira/url: Żądanie nie powiodło się."],
  ])("a refused save with %s shows Polish text, never the server's", async (_name, body, text) => {
    await stubApi({ ...base, "PUT /api/admin/config": () => new Response(JSON.stringify(body), { status: 403 }) });
    renderInLocale("pl", "/admin/config", <Config />);
    await screen.findByText("URL");
    editOf("URL", "Edytuj");
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Wartość" }), { target: { value: "https://y" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Zapisz" }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByText(/server words/)).toBeNull();
  });

  it("a list that cannot be read shows the screen's Polish text", async () => {
    await stubApi({ ...base, "/api/admin/config": () => new Response(JSON.stringify({ error: "server words" }), { status: 403 }) });
    renderInLocale("pl", "/admin/config", <Config />);
    expect(await screen.findByText("Nie udało się wczytać konfiguracji.")).toBeInTheDocument();
    expect(screen.queryByText(/server words/)).toBeNull();
  });
});
