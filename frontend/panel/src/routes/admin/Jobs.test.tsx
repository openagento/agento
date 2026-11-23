import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useLocation, useNavigate } from "react-router";
import { LIVE_REFETCH_MS, PanelModulesProvider } from "@agento/api";
import { SEARCH_DEBOUNCE_MS } from "@agento/ui";
import { MODULES } from "../../generated/registry";
import { Jobs } from "./Jobs";
import { renderAt, stubApi, teardown } from "./testing";

const row = (id: number, status: string, ref: string) => ({
  id, type: "cron", status, source: "jira", reference_id: ref, agent_type: "claude", agent_view_code: "av",
  created_at: "2026-10-02T10:00:00Z", started_at: "2026-10-02T10:00:00Z", finished_at: "2026-10-02T10:00:03Z",
  input_tokens: 1, output_tokens: 2, error_class: null,
});
const pageOf = (rows: unknown[], total = rows.length, page = 1) => ({ rows, total, page, page_size: 50 });
afterEach(() => { teardown(); localStorage.clear(); vi.useRealTimers(); });

const KEY = "agento.admin.jobs.columns";
const headers = () => screen.getAllByRole("columnheader").map((h) => h.textContent);
const DEFAULT = ["ID", "Type", "Status", "Agent view", "Reference", "Created", "Duration", "Actions"];
async function openPicker() {
  fireEvent.click(screen.getByRole("button", { name: "Columns" }));
  return within(await screen.findByRole("dialog", { name: "Columns" }));
}
/** The URL the grid wrote, and a way to move it as Back would. */
function Where({ go }: { go?: string }) {
  const { pathname, search } = useLocation();
  const navigate = useNavigate();
  return <>
    <output aria-label="path">{pathname}</output><output aria-label="url">{search}</output>
    {go && <button onClick={() => void navigate(go)}>go</button>}
  </>;
}
const url = () => screen.getByRole("status", { hidden: true, name: "url" }).textContent;
const sent = (api: Awaited<ReturnType<typeof stubApi>>) => api.calls("GET", "/api/admin/jobs").map((c) => c.url.search);

describe("Jobs", () => {
  it("asks the server for one page with the status, search and sort from the URL", async () => {
    const api = await stubApi({ "/api/admin/jobs": pageOf([row(2, "FAILED", "AG-2")]) });
    renderAt("/admin/jobs?status=FAILED&q=ag&sort=type&dir=asc", <Jobs />);
    expect(await screen.findByText("AG-2")).toBeInTheDocument();
    expect(sent(api)[0]).toBe("?page=1&q=ag&sort=type&dir=asc&status=FAILED");
    expect(screen.getByRole("textbox", { name: "Search jobs" })).toHaveValue("ag");
    expect(screen.getByText("3s")).toBeInTheDocument();
  });

  it.each([
    ["?status=All"], ["?status=BOGUS"], ["?sort=bogus"], ["?sort=id&dir=sideways"], [`?q=${"x".repeat(201)}`],
  ])("a URL value the server would refuse (%s) reads as its default", async (search) => {
    const api = await stubApi({ "/api/admin/jobs": pageOf([row(1, "SUCCESS", "AG-1")]) });
    renderAt(`/admin/jobs${search}`, <Jobs />);
    await screen.findByText("AG-1");
    const asked = new URLSearchParams(sent(api)[0]);
    expect(asked.get("status")).toBeNull();
    expect(asked.get("sort")).toBeNull();
    expect((asked.get("q") ?? "").length).toBeLessThanOrEqual(200);
    expect(screen.getByRole("radio", { name: "All" })).toBeChecked();
  });

  it.each([
    ["?dir=asc", "?page=1&sort=id&dir=asc", "ascending"],
    ["?sort=id&dir=desc", "?page=1", "descending"],
    ["?sort=id", "?page=1", "descending"],
  ])("%s resolves to one sort for the header and the request", async (search, request, ariaSort) => {
    const api = await stubApi({ "/api/admin/jobs": pageOf([row(1, "SUCCESS", "AG-1")]) });
    renderAt(`/admin/jobs${search}`, <Jobs />);
    await screen.findByText("AG-1");
    expect(sent(api)[0]).toBe(request);
    expect(screen.getByRole("columnheader", { name: "ID" })).toHaveAttribute("aria-sort", ariaSort);
  });

  it.each([[""], ["?sort=id&dir=desc"]])("the first ID click from the default (%s) sorts oldest first", async (search) => {
    await stubApi({ "/api/admin/jobs": pageOf([row(1, "SUCCESS", "AG-1")]) });
    renderAt(`/admin/jobs${search}`, <><Jobs /><Where /></>);
    await screen.findByText("AG-1");
    fireEvent.click(screen.getByRole("button", { name: "ID" }));
    await waitFor(() => expect(url()).toBe("?sort=id&dir=asc"));
  });

  describe("the search the server runs", () => {
    const ref = `${"a".repeat(199)}😀`; // 200 code points, 201 UTF-16 units
    it.each([
      ["an emoji at the limit stays whole", ref, ref],
      ["a leading space does not cost a character", ` ${ref}`, ref],
      ["U+0085 is stripped as Python strips it", "\u0085AG-1\u0085", "AG-1"],
      ["U+FEFF is kept as Python keeps it", "\ufeffAG-1", "\ufeffAG-1"],
    ])("%s, from a URL and from the box alike", async (_name, text, expected) => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const api = await stubApi({ "/api/admin/jobs": pageOf([row(1, "SUCCESS", "AG-1")]) });
      renderAt(`/admin/jobs?q=${encodeURIComponent(text)}`, <><Jobs /><Where /></>);
      await screen.findByText("AG-1");
      expect(new URLSearchParams(sent(api)[0]).get("q")).toBe(expected);
      const box = screen.getByRole("textbox", { name: "Search jobs" });
      fireEvent.change(box, { target: { value: "zz" } });
      await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS));
      fireEvent.change(box, { target: { value: text } });
      expect(box).toHaveValue(text);
      await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS));
      // The box writes the same q to the URL, and the URL is what the request is made from (above).
      await waitFor(() => expect(new URLSearchParams(url() ?? "").get("q")).toBe(expected));
    });

    it("a shared URL with a longer search is cut to what the server accepts", async () => {
      const api = await stubApi({ "/api/admin/jobs": pageOf([row(1, "SUCCESS", "AG-1")]) });
      renderAt(`/admin/jobs?q=${encodeURIComponent(`${ref}b`)}`, <Jobs />);
      await screen.findByText("AG-1");
      expect(new URLSearchParams(sent(api)[0]).get("q")).toBe(ref);
    });

    it("a box longer than the server searches says so and sends nothing until it fits", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      await stubApi({ "/api/admin/jobs": pageOf([row(1, "SUCCESS", "AG-1")]) });
      renderAt("/admin/jobs", <><Jobs /><Where /></>);
      await screen.findByText("AG-1");
      const box = screen.getByRole("textbox", { name: "Search jobs" });
      fireEvent.change(box, { target: { value: `${ref}b` } });
      expect(box).toHaveAccessibleDescription("Search at most 200 characters");
      await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS * 2));
      expect(url()).toBe("");
      fireEvent.change(box, { target: { value: ref } });
      expect(box).not.toHaveAccessibleDescription();
      await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS));
      await waitFor(() => expect(new URLSearchParams(url() ?? "").get("q")).toBe(ref));
    });
  });

  it("a header click cycles its sort: first direction, the other one, back to newest first", async () => {
    const api = await stubApi({ "/api/admin/jobs": pageOf([row(1, "SUCCESS", "AG-1")]) });
    renderAt("/admin/jobs", <><Jobs /><Where /></>);
    await screen.findByText("AG-1");
    const click = async (name: string, expected: string) => {
      fireEvent.click(screen.getByRole("button", { name }));
      await waitFor(() => expect(url()).toBe(expected));
    };
    await click("Type", "?sort=type&dir=asc");
    await click("Type", "?sort=type&dir=desc");
    await click("Type", "");
    await click("ID", "?sort=id&dir=asc");
    await click("ID", "");
    expect(sent(api)).toContain("?page=1&sort=type&dir=desc");
  });

  it("All sends no status, and an empty queue says No jobs", async () => {
    const api = await stubApi({ "/api/admin/jobs": pageOf([]) });
    renderAt("/admin/jobs", <Jobs />);
    expect(await screen.findByText("No jobs")).toBeInTheDocument();
    expect(sent(api)[0]).toBe("?page=1");
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });

  it("sends the search after a pause in typing, from page 1", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = await stubApi({ "/api/admin/jobs": pageOf([row(1, "SUCCESS", "AG-1")], 120) });
    renderAt("/admin/jobs?page=2", <><Jobs /><Where /></>);
    await screen.findByText("AG-1");
    fireEvent.change(screen.getByRole("textbox", { name: "Search jobs" }), { target: { value: "ag-1" } });
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(sent(api).some((s) => s.includes("q="))).toBe(false);
    await act(() => vi.advanceTimersByTimeAsync(300));
    await waitFor(() => expect(sent(api).at(-1)).toBe("?page=1&q=ag-1"));
    expect(url()).toBe("?q=ag-1");
  });

  it("a header click sorts on the server and goes back to page 1", async () => {
    const api = await stubApi({ "/api/admin/jobs": pageOf([row(1, "SUCCESS", "AG-1")], 120) });
    renderAt("/admin/jobs?page=2", <><Jobs /><Where /></>);
    await screen.findByText("AG-1");
    fireEvent.click(screen.getByRole("button", { name: "Type" }));
    await waitFor(() => expect(sent(api).at(-1)).toMatch(/^\?page=1&sort=type&dir=(asc|desc)$/));
    const type = screen.getAllByRole("columnheader").find((h) => h.textContent === "Type")!;
    expect(type).toHaveAttribute("aria-sort");
    expect(screen.getAllByRole("columnheader")[0]).not.toHaveAttribute("aria-sort");
  });

  it("pages with Previous and Next, and shows where it is", async () => {
    const api = await stubApi({ "/api/admin/jobs": (_b: unknown, u: URL) => pageOf([row(1, "SUCCESS", "AG-1")], 120, Number(u.searchParams.get("page"))) });
    renderAt("/admin/jobs", <><Jobs /><Where /></>);
    expect(await screen.findByText("1–50 of 120")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("51–100 of 120")).toBeInTheDocument();
    expect(url()).toBe("?page=2");
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("101–120 of 120")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    expect(sent(api)).toContain("?page=3");
  });

  it.each([
    ["status only", "?status=FAILED", "No job matches Status FAILED."],
    ["search only", "?q=abc", "No job matches “abc”."],
    ["both", "?status=DEAD&q=abc", "No job matches Status DEAD and “abc”."],
  ])("an empty %s result names its filters, and Clear filters shows every job", async (_name, query, text) => {
    const api = await stubApi({ "/api/admin/jobs": (_b: unknown, u: URL) =>
      (u.search === "?page=1" ? pageOf([row(1, "SUCCESS", "AG-1")]) : pageOf([])) });
    renderAt(`/admin/jobs${query}`, <><Jobs /><Where /></>);
    expect(await screen.findByText("No matching jobs")).toBeInTheDocument();
    expect(screen.getByText(text)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText("AG-1")).toBeInTheDocument();
    expect(url()).toBe("");
    expect(screen.getByRole("textbox", { name: "Search jobs" })).toHaveValue("");
    expect(sent(api).at(-1)).toBe("?page=1");
  });

  it.each([
    ["rows that left", 3, 60, "?page=2"],
    ["an empty result", 3, 0, ""],
  ])("a page past the end (%s) moves to the last page", async (_name, page, total, expected) => {
    await stubApi({ "/api/admin/jobs": (_b: unknown, u: URL) =>
      pageOf(Number(u.searchParams.get("page")) > Math.max(1, Math.ceil(total / 50)) ? [] : [row(1, "SUCCESS", "AG-1")],
        total, Number(u.searchParams.get("page"))) });
    renderAt(`/admin/jobs?page=${page}`, <><Jobs /><Where /></>);
    await waitFor(() => expect(url()).toBe(expected));
  });

  it("does not move the page while the answer for the new URL is still on its way", async () => {
    let release!: () => void;
    const later = new Promise<void>((r) => { release = r; });
    let asked!: () => void;
    const arrived = new Promise<void>((r) => { asked = r; });
    await stubApi({ "/api/admin/jobs": async (_b: unknown, u: URL) => {
      if (u.searchParams.get("q")) return pageOf([row(5, "FAILED", "AG-5")], 1);
      asked();
      await later;
      return pageOf([row(1, "SUCCESS", "AG-1")], 150, 3);
    } });
    renderAt("/admin/jobs?q=ag-5", <><Jobs /><Where go="/admin/jobs?page=3" /></>);
    await screen.findByText("AG-5");
    fireEvent.click(screen.getByRole("button", { name: "go" }));
    await act(() => arrived);
    expect(url()).toBe("?page=3");
    release();
    expect(await screen.findByText("101–150 of 150")).toBeInTheDocument();
    expect(url()).toBe("?page=3");
  });

  it("Details opens a drawer with prompt, output and the copyable replay command", async () => {
    await stubApi({
      "/api/admin/jobs": pageOf([row(7, "FAILED", "AG-7")]),
      "/api/admin/jobs/7": { ...row(7, "FAILED", "AG-7"), model: "m", error_message: "boom", result_summary: null,
        prompt: "do it", output: "done" },
    });
    renderAt("/admin/jobs", <Jobs />);
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    const drawer = await screen.findByRole("dialog");
    expect(await within(drawer).findByText("do it")).toBeInTheDocument();
    expect(within(drawer).getByText("bin/agento replay 7")).toBeInTheDocument();
    expect(within(drawer).getByText(/Runs in the cron container/)).toBeInTheDocument();
    expect(within(drawer).getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });

  it("?job=<id> opens that job's drawer (the link from a conversation's runs)", async () => {
    await stubApi({
      "/api/admin/jobs": pageOf([]),
      "/api/admin/jobs/9": { ...row(9, "DONE", "AG-9"), model: "m", error_message: null, result_summary: null, prompt: "p9", output: "o9" },
    });
    renderAt("/admin/jobs?job=9", <Jobs />);
    expect(await within(await screen.findByRole("dialog")).findByText("p9")).toBeInTheDocument();
  });

  describe("the drawer's links from modules (the shipped registry)", () => {
    const job = { ...row(9, "SUCCESS", "AG-9"), model: "m", error_message: null, result_summary: null, prompt: "p9", output: "o9" };
    let api!: Awaited<ReturnType<typeof stubApi>>;
    const open = async (answer: () => unknown) => {
      api = await stubApi({ "/api/admin/jobs": pageOf([]), "/api/admin/jobs/9": job, "/api/conversation/jobs/9": answer });
      renderAt("/admin/jobs?job=9", <PanelModulesProvider modules={MODULES}><Jobs /><Where /></PanelModulesProvider>);
      return within(await screen.findByRole("dialog"));
    };
    const missing = () => new Response(JSON.stringify({ error: "not found" }), { status: 404 });

    it("shows Open conversation when the module answers, and it opens the conversation", async () => {
      const drawer = await open(() => ({ id: 41 }));
      fireEvent.click(await drawer.findByRole("button", { name: "Open conversation" }));
      await waitFor(() => expect(screen.getByRole("status", { hidden: true, name: "path" })).toHaveTextContent("/conversations/41"));
    });

    it.each([[{}], [{ id: "41/../../admin" }], [{ id: 0 }], [{ id: 4.5 }], [null]])(
      "shows no link for a 200 without a positive integer id (%j)", async (answer) => {
        const drawer = await open(() => answer);
        await drawer.findByText("p9");
        await waitFor(() => expect(api.calls("GET", "/api/conversation/jobs/9")).toHaveLength(1));
        await act(async () => {}); // the answer settles
        expect(drawer.queryByRole("button", { name: "Open conversation" })).toBeNull();
      });

    it("hides it on a 404, and follows the answer while the drawer stays open", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      let found = false;
      const drawer = await open(() => (found ? { id: 41 } : missing()));
      await drawer.findByText("p9");
      expect(drawer.queryByRole("button", { name: "Open conversation" })).toBeNull();
      found = true;
      await act(() => vi.advanceTimersByTimeAsync(LIVE_REFETCH_MS));
      expect(await drawer.findByRole("button", { name: "Open conversation" })).toBeInTheDocument();
      found = false;
      await act(() => vi.advanceTimersByTimeAsync(LIVE_REFETCH_MS));
      await waitFor(() => expect(drawer.queryByRole("button", { name: "Open conversation" })).toBeNull());
    });
  });

  describe("column picker", () => {
    const show = async (stored?: string) => {
      if (stored !== undefined) localStorage.setItem(KEY, stored);
      await stubApi({ "/api/admin/jobs": pageOf([row(1, "SUCCESS", "AG-1")]) });
      renderAt("/admin/jobs", <Jobs />);
      expect(await screen.findAllByRole("row")).toHaveLength(2);
    };

    it("shows today's columns by default", async () => {
      await show();
      expect(headers()).toEqual(DEFAULT);
    });

    it("adds and removes columns live, in one order, and keeps the choice", async () => {
      await show();
      const picker = await openPicker();
      fireEvent.click(picker.getByRole("checkbox", { name: "Input tokens" }));
      fireEvent.click(picker.getByRole("checkbox", { name: "Source" }));
      fireEvent.click(picker.getByRole("checkbox", { name: "Type" }));
      expect(headers()).toEqual(["ID", "Status", "Agent view", "Source", "Reference", "Created", "Duration", "Input tokens", "Actions"]);
      expect(screen.getByText("jira")).toBeInTheDocument();
      expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual(["id", "status", "view", "source", "ref", "created", "duration", "input_tokens"]);
    });

    it("reads a stored choice on the first render", async () => {
      await show(JSON.stringify(["error_class", "id"]));
      expect(headers()).toEqual(["ID", "Error class", "Actions"]);
    });

    it.each([["{"], ['["nope"]'], ['"id"'], ["[]"]])("falls back to the default for a stored %s", async (stored) => {
      await show(stored);
      expect(headers()).toEqual(DEFAULT);
    });

    it("keeps the last column: its checkbox is disabled", async () => {
      await show(JSON.stringify(["ref"]));
      const picker = await openPicker();
      expect(picker.getByRole("checkbox", { name: "Reference" })).toBeDisabled();
      expect(picker.getByRole("checkbox", { name: "Reference" })).toHaveAccessibleDescription("At least one column stays visible");
      expect(picker.getByRole("checkbox", { name: "ID" })).toBeEnabled();
    });

    it("Show all columns and Reset columns, each disabled when it changes nothing", async () => {
      await show();
      const picker = await openPicker();
      expect(picker.getByRole("button", { name: "Reset columns" })).toBeDisabled();
      fireEvent.click(picker.getByRole("button", { name: "Show all columns" }));
      expect(headers()).toHaveLength(15);
      expect(picker.getByRole("button", { name: "Show all columns" })).toBeDisabled();
      fireEvent.click(picker.getByRole("button", { name: "Reset columns" }));
      expect(headers()).toEqual(DEFAULT);
    });
  });
});
