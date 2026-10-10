// The save contract Tools and Skills share: a draft per scope, one batch on Save, nothing written
// before it, and the draft kept whenever the write did not land. Run against both screens, because
// they differ only in their list key and URL — and that difference is exactly what a wrong
// invalidation gets wrong.
import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Skills } from "./Skills";
import { Tools } from "./Tools";
import { renderAt, stubApi, teardown } from "./testing";

afterEach(teardown);

const SCOPES = { workspaces: [{ id: 2, code: "ws", label: "WS", is_active: true }],
  agent_views: [{ id: 3, code: "av", label: "AV", workspace_id: 2, is_active: true }] };

interface Row { name: string; path: string; enabled: boolean; explicit_here: boolean; blocked_by?: string | null }
const row = (name: string, path: string, enabled: boolean): Row => ({ name, path, enabled, explicit_here: true });

/** The two screens, each with its own URL, list key, wire shape and a stateful server per scope. */
const SCREENS = [
  {
    name: "Tools", ui: <Tools />, url: "/admin/tools", api: "/api/admin/tools",
    rows: () => [row("jira_search", "tools/jira_search/is_enabled", false), row("jira_get", "tools/jira_get/is_enabled", true)],
    wire: (rows: Row[]) => [{ toolset: "jira", tools: rows }],
  },
  {
    name: "Skills", ui: <Skills />, url: "/admin/skills", api: "/api/admin/skills",
    rows: () => [row("pdf", "skill/pdf/is_enabled", false), row("xlsx", "skill/xlsx/is_enabled", true)],
    wire: (rows: Row[]) => rows,
  },
];

const DEFAULT = "scope=default&scope_id=0";
const WS = "scope=workspace&scope_id=2";

const save = () => fireEvent.click(screen.getByRole("button", { name: "Save" }));
const pick = async (kind: "Workspace" | "Agent view", option: string) => {
  fireEvent.click(screen.getByRole("radio", { name: kind }));
  fireEvent.click(await screen.findByPlaceholderText("Choose…"));
  fireEvent.click(await screen.findByRole("option", { name: option }));
};

describe.each(SCREENS)("$name save contract", ({ ui, url, api, rows, wire }) => {
  const first = () => rows()[0].name;
  const second = () => rows()[1].name;
  const firstPath = () => rows()[0].path;

  /** A server that remembers writes, per scope — a double that lies about the write would let a
   *  wrongly cleared draft pass as "saved" (TST-3). A GET of one scope and the PUT can each be held
   *  open, which is the only way to put a save and a scope change in a chosen order. */
  const server = (opts: { fail?: string[] } = {}) => {
    const state = new Map<string, Row[]>();
    const held = new Map<string, () => void>();
    const gates = new Map<string, Promise<void>>();
    const breaks = new Set<string>();
    const of = (search: string) => {
      if (!state.has(search)) state.set(search, rows());
      return state.get(search)!;
    };
    const hold = (key: string) => {
      gates.set(key, new Promise<void>((r) => held.set(key, () => { gates.delete(key); r(); })));
    };
    const gate = (key: string) => gates.get(key);
    return {
      answers: {
        "/api/admin/scopes": SCOPES,
        [api]: (_b: unknown, u: URL) => {
          const search = u.search.slice(1);
          const answer = () => breaks.has(search)
            ? new Response(JSON.stringify({ error: "Gone" }), { status: 400 })
            : wire(of(search));
          const g = gate(`GET ${search}`);
          return g ? g.then(answer) : answer();
        },
        "PUT /api/admin/config": (b: { path: string; value: string; scope: string; scope_id: number }) => {
          const write = () => {
            if (opts.fail?.includes(b.path)) return new Response(JSON.stringify({ error: "Refused" }), { status: 400 });
            const r = of(`scope=${b.scope}&scope_id=${b.scope_id}`).find((x) => x.path === b.path);
            if (r) r.enabled = b.value === "1";
            return { path: b.path, reset: [] };
          };
          const g = gate("PUT");
          return g ? g.then(write) : write();
        },
      },
      holdGet: (search: string) => hold(`GET ${search}`),
      releaseGet: (search: string) => held.get(`GET ${search}`)!(),
      holdPut: () => hold("PUT"),
      releasePut: () => held.get("PUT")!(),
      /** Every GET of this scope answers 400 from now on — a status the query client does not retry. */
      breakGet: (search: string) => breaks.add(search),
      healGet: (search: string) => breaks.delete(search),
      set: (search: string, path: string, enabled: boolean) => { of(search).find((r) => r.path === path)!.enabled = enabled; },
      enabled: (search: string, path: string) => of(search).find((r) => r.path === path)?.enabled,
    };
  };

  it("writes nothing until Save, then sends only the changed paths", async () => {
    const s = server();
    const api2 = await stubApi(s.answers);
    renderAt(`${url}?scope=workspace&scope_id=2`, ui);
    fireEvent.click(await screen.findByRole("checkbox", { name: first() }));
    expect(api2.calls("PUT", "/api/admin/config")).toEqual([]);

    save();
    await vi.waitFor(() => expect(api2.calls("PUT", "/api/admin/config")).toHaveLength(1));
    expect(api2.calls("PUT", "/api/admin/config")[0].body)
      .toEqual({ path: firstPath(), value: "1", scope: "workspace", scope_id: 2 });
    await vi.waitFor(() => expect(screen.queryByText(/not saved/)).not.toBeInTheDocument());
    expect(await screen.findByRole("checkbox", { name: first() })).toBeChecked();
  });

  it("keeps the draft when the whole batch fails", async () => {
    const s = server({ fail: [rows()[0].path, rows()[1].path] });
    await stubApi(s.answers);
    renderAt(url, ui);
    fireEvent.click(await screen.findByRole("checkbox", { name: first() }));
    fireEvent.click(screen.getByRole("checkbox", { name: second() }));
    save();
    expect(await screen.findByText(/Saved 0 of 2/)).toBeInTheDocument();
    expect(screen.getByText("2 changes not saved")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: first() })).toBeChecked();
  });

  it("keeps the draft when one write of the batch fails, minus the one that landed", async () => {
    const s = server({ fail: [rows()[0].path] });
    await stubApi(s.answers);
    renderAt(url, ui);
    fireEvent.click(await screen.findByRole("checkbox", { name: first() }));
    fireEvent.click(screen.getByRole("checkbox", { name: second() }));
    save();
    expect(await screen.findByText(/Saved 1 of 2/)).toBeInTheDocument();
    // The one that landed is now the server's state; the refused one stays in the draft to retry.
    await vi.waitFor(() => expect(screen.getByText("1 change not saved")).toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: first() })).toBeChecked();
    expect(s.enabled(DEFAULT, rows()[1].path)).toBe(false);
  });

  it("clears the draft only once the saved scope has refetched", async () => {
    const s = server();
    await stubApi(s.answers);
    renderAt(url, ui);
    fireEvent.click(await screen.findByRole("checkbox", { name: first() }));
    s.holdGet(DEFAULT);
    save();
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByText("1 change not saved")).toBeInTheDocument();
    s.releaseGet(DEFAULT);
    await vi.waitFor(() => expect(screen.queryByText(/not saved/)).not.toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: first() })).toBeChecked();
  });

  it("a draft belongs to its scope: another scope keeps its own, and coming back restores it", async () => {
    const s = server();
    await stubApi(s.answers);
    renderAt(`${url}?scope=workspace&scope_id=2`, ui);
    fireEvent.click(await screen.findByRole("checkbox", { name: first() }));

    await pick("Agent view", "av — AV");
    await vi.waitFor(() => expect(screen.getByRole("checkbox", { name: first() })).not.toBeChecked());
    expect(screen.queryByText(/not saved/)).not.toBeInTheDocument();

    // Back with B untouched: A's draft is still there.
    await pick("Workspace", "ws — WS");
    await vi.waitFor(() => expect(screen.getByText("1 change not saved")).toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: first() })).toBeChecked();
  });

  it("editing and saving another scope leaves this scope's draft and writes only that scope", async () => {
    const s = server();
    const api2 = await stubApi(s.answers);
    renderAt(`${url}?scope=workspace&scope_id=2`, ui);
    fireEvent.click(await screen.findByRole("checkbox", { name: first() }));

    await pick("Agent view", "av — AV");
    await vi.waitFor(() => expect(screen.getByRole("checkbox", { name: second() })).toBeChecked());
    fireEvent.click(screen.getByRole("checkbox", { name: second() }));
    save();
    await vi.waitFor(() => expect(api2.calls("PUT", "/api/admin/config")).toHaveLength(1));
    expect(api2.calls("PUT", "/api/admin/config")[0].body)
      .toEqual({ path: rows()[1].path, value: "0", scope: "agent_view", scope_id: 3 });
    expect(s.enabled(WS, rows()[1].path)).toBe(true);

    await pick("Workspace", "ws — WS");
    await vi.waitFor(() => expect(screen.getByText("1 change not saved")).toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: first() })).toBeChecked();
  });

  it("a save of a scope that is no longer on screen still refetches it before the draft clears", async () => {
    const s = server();
    const api2 = await stubApi(s.answers);
    renderAt(`${url}?scope=workspace&scope_id=2`, ui);
    fireEvent.click(await screen.findByRole("checkbox", { name: first() }));
    // Hold the PUT so the save completes only once this scope has left the screen: its refetch then
    // has to reach an INACTIVE query, which is what the default refetchType would silently skip.
    s.holdPut();
    save();
    await pick("Agent view", "av — AV");
    s.holdGet(WS);
    s.releasePut();
    await vi.waitFor(() => expect(s.enabled(WS, firstPath())).toBe(true));

    await pick("Workspace", "ws — WS");
    // Still held, so nothing has refreshed this scope yet: the draft must still be here.
    expect(screen.getByText("1 change not saved")).toBeInTheDocument();
    s.releaseGet(WS);
    await vi.waitFor(() => expect(screen.queryByText(/not saved/)).not.toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: first() })).toBeChecked();
    expect(api2.calls("PUT", "/api/admin/config")).toHaveLength(1);
  });

  it("keeps the draft when the save landed but the refetch failed", async () => {
    const s = server();
    await stubApi(s.answers);
    renderAt(url, ui);
    fireEvent.click(await screen.findByRole("checkbox", { name: first() }));
    s.breakGet(DEFAULT);
    save();
    await vi.waitFor(() => expect(s.enabled(DEFAULT, firstPath())).toBe(true));
    // The write landed, the refresh did not. The screen cannot show what is stored, so the draft is
    // the only record of what the user did — it must survive until a refresh actually succeeds.
    expect(await screen.findByText("Gone")).toBeInTheDocument();
    // Someone else puts the value back before this screen could read it, so the next successful
    // refresh disagrees with the save — only a draft that was NOT cleared can still show the intent.
    s.set(DEFAULT, firstPath(), false);
    s.healGet(DEFAULT);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("1 change not saved")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: first() })).toBeChecked();
  });

  it("holds a draft for each scope at once, and Reset drops only this scope's", async () => {
    const s = server();
    await stubApi(s.answers);
    renderAt(`${url}?scope=workspace&scope_id=2`, ui);
    fireEvent.click(await screen.findByRole("checkbox", { name: first() }));

    await pick("Agent view", "av — AV");
    await vi.waitFor(() => expect(screen.getByRole("checkbox", { name: second() })).toBeChecked());
    fireEvent.click(screen.getByRole("checkbox", { name: second() }));
    expect(screen.getByText("1 change not saved")).toBeInTheDocument();

    await pick("Workspace", "ws — WS");
    await vi.waitFor(() => expect(screen.getByRole("checkbox", { name: first() })).toBeChecked());
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(screen.queryByText(/not saved/)).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: first() })).not.toBeChecked();

    // The other scope's unsaved draft survived this scope's Reset.
    await pick("Agent view", "av — AV");
    await vi.waitFor(() => expect(screen.getByText("1 change not saved")).toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: second() })).not.toBeChecked();
  });
});
