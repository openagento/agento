import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { queryClient } from "@agento/api";
import { ToolCatalog } from "./ToolCatalog";
import { CATALOG_KEY } from "./ToolLabels";
import { renderAt, stubApi, teardown } from "./testing";

afterEach(teardown);

const LABELS = [
  { id: 1, key: "private-data", name: "Private data access", protected: true, usage: 2 },
  { id: 2, key: "untrusted-content", name: "Untrusted content", protected: true, usage: 0 },
  { id: 3, key: "external-communication", name: "External communication", protected: true, usage: 0 },
  { id: 4, key: "stub-in-test", name: "Stub in test", protected: true, usage: 0 },
  { id: 5, key: "finance", name: "Finance", protected: false, usage: 2 },
];
const item = (id: number, name: string, source: string, labels: string[], reviewed = false, revision = 1) => ({
  name, path: `tools/${name}/is_enabled`, enabled: true, explicit_here: true, blocked_by: null, requires: null,
  catalog: { id, source: `module:${source}`, labels, reviewed, revision },
});
const catalog = (revision = 1) => [
  { toolset: "mail", tools: [
    item(1, "mail_read", "mail", ["finance", "private-data"], false, revision),
    item(2, "mail_list", "mail", ["private-data"], true),
  ] },
  { toolset: "erp", tools: [item(3, "erp_invoice", "erp", ["finance"])] },
];
const answers = (extra: Record<string, unknown> = {}) => ({
  "/api/admin/tools": () => catalog(), "/api/admin/tool-labels": LABELS, ...extra,
});

// The Tool cell holds the name, then the module and the labels under it.
const rowNames = () => screen.getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell")[0].querySelector("p")!.textContent);
const pickLabel = async (name: string) => {
  fireEvent.click(screen.getByRole("combobox", { name: "Labels" }));
  fireEvent.click(await screen.findByRole("option", { name }));
};
const openDrawer = async (tool: string) => {
  const row = (await screen.findByText(tool)).closest("tr")!;
  fireEvent.click(within(row).getByRole("button", { name: "Classify" }));
  return screen.findByRole("dialog");
};
const addChip = (dialog: HTMLElement, text: string) => {
  const input = within(dialog).getByRole("combobox", { name: "Labels" });
  input.focus();
  fireEvent.change(input, { target: { value: text } });
  fireEvent.keyDown(input, { key: "Enter" });
};
/** The PATCH answer the server gives (tool_catalog.classify_tool): the keys it saved, a new label's
 *  key among them, the review state (`review: null` keeps the tool's own) and the next revision. */
const saved = (catalogId: number, revision: number, wasReviewed = false) => (body: unknown) => {
  const { labels, new_label, review } = body as { labels: string[]; new_label: string | null; review: string | null };
  const keys = new_label ? [...labels, new_label.toLowerCase().replace(/[^a-z0-9]+/g, "-")] : labels;
  return { id: catalogId, source: "module:mail", labels: keys.sort(), reviewed: review === null ? wasReviewed : review === "reviewed", revision };
};
/** The catalog with a third tool to review, for a run that goes through more than one "next". */
const threeToReview = () => {
  const tools = catalog();
  tools[1].tools.push(item(4, "erp_pay", "erp", []));
  return tools;
};

describe("ToolCatalog", () => {
  it("filters AND labels", async () => {
    await stubApi(answers());
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    await screen.findByText("erp_invoice");
    await pickLabel("Finance");
    expect(rowNames()).toEqual(["mail_read", "erp_invoice"]);
    await pickLabel("Private data access");
    expect(rowNames()).toEqual(["mail_read"]);
    // AND with the status filter too: mail_read is still to review.
    fireEvent.click(screen.getByRole("radio", { name: /^Reviewed/ }));
    expect(screen.getByText("No matching tools")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(rowNames()).toHaveLength(3);
  });

  it("create chip and assign in one save", async () => {
    const api = await stubApi(answers({ "PATCH /api/admin/tools/2/classification": saved(2, 2, true) }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const dialog = await openDrawer("mail_list");
    addChip(dialog, "Payroll");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved mail_list.")).toBeInTheDocument();
    expect(api.calls("POST", "/api/admin/tool-labels")).toHaveLength(0);
    expect(api.calls("PATCH", "/api/admin/tools/2/classification").map((c) => c.body)).toEqual([
      { revision: 1, labels: ["private-data"], new_label: "Payroll", review: null },
    ]);
  });

  it("two new chips: the extra one is created first, then one save names every key", async () => {
    const api = await stubApi(answers({
      "POST /api/admin/tool-labels": (b: { name: string }) => ({ id: 9, key: b.name.toLowerCase(), name: b.name, protected: false, usage: 0 }),
      "PATCH /api/admin/tools/2/classification": saved(2, 2, true),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const dialog = await openDrawer("mail_list");
    addChip(dialog, "Payroll");
    addChip(dialog, "Legal");
    // mail_list is reviewed: only a CHANGE of the review status is sent.
    fireEvent.click(within(dialog).getByRole("radio", { name: "To review" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved mail_list.")).toBeInTheDocument();
    expect(api.calls("POST", "/api/admin/tool-labels").map((c) => c.body)).toEqual([{ name: "Legal" }]);
    expect(api.calls("PATCH", "/api/admin/tools/2/classification")[0].body).toEqual(
      { revision: 1, labels: ["private-data", "legal"], new_label: "Payroll", review: "to_review" });
  });

  it("409 refetches", async () => {
    let revision = 1;
    const api = await stubApi(answers({
      "/api/admin/tools": () => catalog(revision),
      "PATCH /api/admin/tools/1/classification": () => {
        revision = 2;
        return new Response(JSON.stringify({ error: "the tool was changed since it was read; reload it" }), { status: 409 });
      },
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const dialog = await openDrawer("mail_read");
    const before = api.calls("GET", "/api/admin/tools").length;
    addChip(dialog, "Untrusted content");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/changed since you opened it/)).toBeInTheDocument();
    expect(await within(dialog).findByText(/Changed on the server/)).toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/tools").length).toBeGreaterThan(before);
  });

  it("refetch keeps drawer edits and shows changed notice", async () => {
    let revision = 1;
    const api = await stubApi(answers({
      "/api/admin/tools": () => catalog(revision),
      "PATCH /api/admin/tools/1/classification": saved(1, 3),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const dialog = await openDrawer("mail_read");
    addChip(dialog, "Untrusted content");
    revision = 2;
    await act(() => queryClient.invalidateQueries({ queryKey: CATALOG_KEY }));
    expect(await within(dialog).findByText(/Changed on the server/)).toBeInTheDocument();
    expect(within(dialog).getByText("Untrusted content", { selector: ".mantine-Pill-label" })).toBeInTheDocument();
    // Save still sends the revision the form was opened on: the server decides (409).
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved mail_read.")).toBeInTheDocument();
    expect(api.calls("PATCH", "/api/admin/tools/1/classification")[0].body).toMatchObject({ revision: 1 });
  });

  it("Reload starts the form over from the server's row", async () => {
    let revision = 1;
    await stubApi(answers({ "/api/admin/tools": () => catalog(revision) }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const dialog = await openDrawer("mail_read");
    addChip(dialog, "Untrusted content");
    revision = 2;
    await act(() => queryClient.invalidateQueries({ queryKey: CATALOG_KEY }));
    fireEvent.click(await within(dialog).findByRole("button", { name: "Reload" }));
    expect(within(dialog).queryByText(/Changed on the server/)).not.toBeInTheDocument();
    expect(within(dialog).queryByText("Untrusted content", { selector: ".mantine-Pill-label" })).not.toBeInTheDocument();
  });

  it("the drawer waits for the labels, then starts from their names with nothing to save", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    await stubApi(answers({ "/api/admin/tool-labels": () => gate.then(() => LABELS) }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const dialog = await openDrawer("mail_read");
    // Without the labels, "private-data" would read as a new label and Save would send it.
    expect(within(dialog).queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    await act(async () => { release(); await gate; });
    expect(await within(dialog).findByText("Finance", { selector: ".mantine-Pill-label" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("the Tool cell holds the review state, module and labels, so a phone shows the Classify action", async () => {
    await stubApi(answers());
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const cells = within((await screen.findByText("mail_read")).closest("tr")!).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("mail");
    expect(within(cells[0]).getByText("Finance")).toBeInTheDocument();
    expect(cells[0]).toHaveTextContent("To review");
    expect(within(cells[1]).getByRole("button", { name: "Classify" })).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Tool", "Actions"]);
  });

  it("the banner counts the tools to review; Start review opens the first at Reviewed", async () => {
    await stubApi(answers());
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    expect(await screen.findByText("2 tools to review")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start review" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("mail_read")).toBeInTheDocument();
    expect(within(dialog).getByRole("radio", { name: "Reviewed" })).toBeChecked();
    expect(within(dialog).getByText("2 left")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Save and next" })).toBeEnabled();
    // The input comes last, so its open dropdown covers neither the decision nor any hint: the
    // review control is above it, and the hints (risk, --stub-tools keys) are its description.
    const input = within(dialog).getByRole("combobox", { name: "Labels" });
    const reviewed = within(dialog).getByRole("radio", { name: "Reviewed" });
    expect(reviewed.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const hint = within(dialog).getByText(/Pick a label/);
    expect(hint.className).toMatch(/description/);
    expect(hint).toHaveTextContent("an agent with all three can be made to leak private data");
    expect(hint).toHaveTextContent("Keys for --stub-tools: finance, private-data");
    expect(document.activeElement).not.toBe(input);
  });

  it("Save and next goes through the run; the last save ends it while the refetch is still pending", async () => {
    let hold: Promise<unknown> | null = null;
    const api = await stubApi(answers({
      "/api/admin/tools": () => hold ?? threeToReview(),
      "PATCH /api/admin/tools/1/classification": saved(1, 2),
      "PATCH /api/admin/tools/3/classification": saved(3, 2),
      "PATCH /api/admin/tools/4/classification": saved(4, 2),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    fireEvent.click(await screen.findByRole("button", { name: "Start review" }));
    hold = new Promise(() => {});
    let dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("erp_invoice")).toBeInTheDocument();
    expect(within(dialog).getByText("2 left")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("erp_pay")).toBeInTheDocument();
    expect(within(dialog).getByText("1 left")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    expect(await screen.findByText("All tools are reviewed.")).toBeInTheDocument();
    expect(screen.queryByText(/^Saved /)).not.toBeInTheDocument();
    expect([1, 3, 4].map((id) => api.calls("PATCH", `/api/admin/tools/${id}/classification`)[0].body)).toEqual([
      { revision: 1, labels: ["finance", "private-data"], new_label: null, review: "reviewed" },
      { revision: 1, labels: ["finance"], new_label: null, review: "reviewed" },
      { revision: 1, labels: [], new_label: null, review: "reviewed" },
    ]);
  });

  it("a tool saved back to To review leaves the run paused, not done", async () => {
    await stubApi(answers({
      "PATCH /api/admin/tools/1/classification": saved(1, 2),
      "PATCH /api/admin/tools/3/classification": saved(3, 2),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    fireEvent.click(await screen.findByRole("button", { name: "Start review" }));
    let dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("erp_invoice");
    addChip(dialog, "Untrusted content");
    fireEvent.click(within(dialog).getByRole("radio", { name: "To review" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    expect(await screen.findByText("Review paused. 1 tool to review.")).toBeInTheDocument();
  });

  it("Skip saves nothing", async () => {
    const api = await stubApi(answers());
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    fireEvent.click(await screen.findByRole("button", { name: "Start review" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Skip" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("erp_invoice");
    fireEvent.click(within(dialog).getByRole("button", { name: "Skip" }));
    expect(await screen.findByText("Review paused. 2 tools to review.")).toBeInTheDocument();
    expect([1, 3].flatMap((id) => api.calls("PATCH", `/api/admin/tools/${id}/classification`))).toHaveLength(0);
  });

  it("a save that answers after the drawer closed does not reopen it; the form is locked while it runs", async () => {
    let answer: (v: unknown) => void = () => undefined;
    await stubApi(answers({
      "PATCH /api/admin/tools/1/classification": (body: unknown) => new Promise((r) => { answer = () => r(saved(1, 2)(body)); }),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    fireEvent.click(await screen.findByRole("button", { name: "Start review" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    // The lock shows once the mutation's state reaches the form (tanstack notifies on a timer).
    await waitFor(() => expect(within(dialog).getByRole("radio", { name: "To review" })).toBeDisabled());
    fireEvent.keyDown(dialog, { key: "Escape" });
    await act(async () => { answer(null); });
    await act(async () => {});
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText(/All tools are reviewed|Review paused/)).not.toBeInTheDocument();
  });

  it("a save from a closed run does not move a new run that shows the same tool", async () => {
    let answer: (v: unknown) => void = () => undefined;
    await stubApi(answers({
      "PATCH /api/admin/tools/1/classification": (body: unknown) => new Promise((r) => { answer = () => r(saved(1, 2)(body)); }),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const start = await screen.findByRole("button", { name: "Start review" });
    fireEvent.click(start);
    let dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    await act(async () => {});
    fireEvent.keyDown(dialog, { key: "Escape" });
    await act(async () => {});
    fireEvent.click(start);
    dialog = await screen.findByRole("dialog");
    addChip(dialog, "New draft");
    await act(async () => { answer(null); });
    await act(async () => {});
    expect(dialog).toHaveAccessibleName("mail_read");
    expect(within(dialog).getByText("New draft")).toBeInTheDocument();
  });

  it("a manual save that answers late does not close a review run's drawer", async () => {
    let answer: (v: unknown) => void = () => undefined;
    await stubApi(answers({
      "PATCH /api/admin/tools/3/classification": (body: unknown) => new Promise((r) => { answer = () => r(saved(3, 2)(body)); }),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    let dialog = await openDrawer("erp_invoice");
    fireEvent.click(within(dialog).getByRole("radio", { name: "Reviewed" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await act(async () => {});
    fireEvent.keyDown(dialog, { key: "Escape" });
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "Start review" }));
    dialog = await screen.findByRole("dialog");
    addChip(dialog, "New draft");
    await act(async () => { answer(null); });
    await act(async () => {});
    expect(screen.getByRole("dialog")).toHaveAccessibleName("mail_read");
    expect(within(dialog).getByText("New draft")).toBeInTheDocument();
  });

  it("a chip resolves by a label's name before another label's key", async () => {
    // "Finance" was renamed "Zebra" (key finance); a new "Finance" got key finance-2. Name order.
    const labels = [{ id: 6, key: "finance-2", name: "Finance", protected: false, usage: 1 }, { ...LABELS[4], name: "Zebra" }];
    const api = await stubApi(answers({
      "/api/admin/tool-labels": [...LABELS.slice(0, 4), ...labels],
      "/api/admin/tools": () => [{ toolset: "erp", tools: [item(3, "erp_invoice", "erp", ["finance-2"])] }],
      "PATCH /api/admin/tools/3/classification": saved(3, 2),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    fireEvent.click(await screen.findByRole("button", { name: "Start review" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Finance")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    await screen.findByText("All tools are reviewed.");
    expect(api.calls("PATCH", "/api/admin/tools/3/classification")[0].body).toMatchObject({ labels: ["finance-2"] });
  });

  it("the keyboard stays in the drawer when the run moves to the next tool", async () => {
    await stubApi(answers({ "PATCH /api/admin/tools/1/classification": saved(1, 2) }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    fireEvent.click(await screen.findByRole("button", { name: "Start review" }));
    let dialog = await screen.findByRole("dialog");
    const skip = within(dialog).getByRole("button", { name: "Skip" });
    skip.focus();
    fireEvent.click(skip);
    dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("erp_invoice");
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("a selected label wraps in the drawer and the filter instead of being cut", async () => {
    await stubApi(answers());
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    await screen.findByText("mail_read");
    await pickLabel("Finance");
    const wraps = (pill: Element | null) => expect(pill?.className).toMatch(/_pill_/);
    wraps(screen.getAllByText("Finance").map((e) => e.closest(".mantine-Pill-root")).find(Boolean)!);
    const dialog = await openDrawer("mail_read");
    wraps(within(dialog).getByText("Finance").closest(".mantine-Pill-root"));
  });

  it("a label renamed while the drawer is open stays the same label", async () => {
    const review = { id: 7, key: "finance-review", name: "Finance Review", protected: false, usage: 1 };
    let labels = [...LABELS, review];
    const api = await stubApi(answers({
      "/api/admin/tool-labels": () => labels,
      "/api/admin/tools": () => [{ toolset: "mail", tools: [item(1, "mail_read", "mail", ["finance-review"])] }],
      "PATCH /api/admin/tools/1/classification": saved(1, 2),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    fireEvent.click(await screen.findByRole("button", { name: "Start review" }));
    const dialog = await screen.findByRole("dialog");
    labels = [...LABELS, { ...review, name: "Zebra" }]; // another admin renames it
    await act(async () => { await queryClient.refetchQueries(); });
    await waitFor(() => expect(within(dialog).getByText("Zebra").closest(".mantine-Pill-root")).not.toBeNull());
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    await screen.findByText("All tools are reviewed.");
    expect(api.calls("PATCH", "/api/admin/tools/1/classification")[0].body)
      .toMatchObject({ labels: ["finance-review"], new_label: null });
  });

  it("text left in the input without Enter adds no label", async () => {
    const api = await stubApi(answers({ "PATCH /api/admin/tools/1/classification": saved(1, 2) }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    fireEvent.click(await screen.findByRole("button", { name: "Start review" }));
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByRole("combobox", { name: "Labels" });
    input.focus();
    fireEvent.change(input, { target: { value: "Untrust" } });
    fireEvent.blur(input);
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    await waitFor(() => expect(api.calls("PATCH", "/api/admin/tools/1/classification")).toHaveLength(1));
    expect(api.calls("PATCH", "/api/admin/tools/1/classification")[0].body).toMatchObject({ new_label: null });
  });

  it("a comma or a paste edits the text; it adds no label", async () => {
    await stubApi(answers());
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const dialog = await openDrawer("erp_invoice");
    const input = within(dialog).getByRole("combobox", { name: "Labels" }) as HTMLInputElement;
    input.focus();
    fireEvent.change(input, { target: { value: "Untrust" } });
    fireEvent.keyDown(input, { key: "," });
    fireEvent.paste(input, { clipboardData: { getData: () => "Pasted" } });
    expect(dialog.querySelectorAll(".mantine-Pill-root")).toHaveLength(1);
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("a label typed by its key while selected stays one chip", async () => {
    await stubApi(answers());
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const dialog = await openDrawer("mail_read");
    addChip(dialog, "private-data");
    expect(within(dialog).getAllByText("Private data access", { selector: ".mantine-Pill-label" })).toHaveLength(1);
  });

  it("a save that fails after its drawer closed gives no advice to the drawer shown now", async () => {
    let answer: () => void = () => undefined;
    await stubApi(answers({
      "PATCH /api/admin/tools/3/classification": () => new Promise((r) => {
        answer = () => r(new Response(JSON.stringify({ error: "changed" }), { status: 409 }));
      }),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    const dialog = await openDrawer("erp_invoice");
    fireEvent.click(within(dialog).getByRole("radio", { name: "Reviewed" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await act(async () => {});
    fireEvent.keyDown(dialog, { key: "Escape" });
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "Start review" }));
    await screen.findByRole("dialog");
    await act(async () => { answer(); });
    await act(async () => {});
    expect(screen.queryByText(/Reload it, then save again/)).not.toBeInTheDocument();
  });

  it("a sync that sends a saved tool back to review counts it again", async () => {
    let revision = 1;
    await stubApi(answers({
      "/api/admin/tools": () => catalog(revision),
      "PATCH /api/admin/tools/1/classification": (body: unknown) => {
        const answer = saved(1, 2)(body);
        revision = 3; // after this save, a sync changed mail_read's contract: unreviewed at revision 3
        return answer;
      },
      "PATCH /api/admin/tools/3/classification": saved(3, 2),
    }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    fireEvent.click(await screen.findByRole("button", { name: "Start review" }));
    let dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("erp_invoice");
    await act(async () => { await queryClient.refetchQueries({ queryKey: CATALOG_KEY }); });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save and next" }));
    expect(await screen.findByText("Review paused. 1 tool to review.")).toBeInTheDocument();
  });

  it("after a header sort, Start review goes in catalog order and the table shows it", async () => {
    await stubApi(answers());
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    await screen.findByText("erp_invoice");
    fireEvent.click(screen.getByRole("button", { name: "Tool" }));
    expect(rowNames()).toEqual(["erp_invoice", "mail_list", "mail_read"]);
    fireEvent.click(screen.getByRole("button", { name: "Start review" }));
    expect(within(await screen.findByRole("dialog")).getByText("mail_read")).toBeInTheDocument();
    expect(rowNames()).toEqual(["mail_read", "erp_invoice"]);
  });

  it("before the first sync, names the command that fills the catalog", async () => {
    await stubApi(answers({ "/api/admin/tools": [{ toolset: "mail", tools: [{ ...item(1, "mail_read", "mail", []), catalog: null }] }] }));
    renderAt("/admin/tools?tab=catalog", <ToolCatalog />);
    expect(await screen.findByText("The catalog is empty")).toBeInTheDocument();
    expect(screen.getByText("bin/agento tool:sync-catalog")).toBeInTheDocument();
  });
});
