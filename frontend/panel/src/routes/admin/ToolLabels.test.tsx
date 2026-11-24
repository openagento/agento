import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ToolLabels } from "./ToolLabels";
import { renderAt, stubApi, teardown } from "./testing";

afterEach(teardown);

const LABELS = [
  { id: 1, key: "private-data", name: "Private data access", protected: true, usage: 4 },
  { id: 5, key: "finance", name: "Finance", protected: false, usage: 3 },
];
const row = async (name: string) => (await screen.findByText(name)).closest("tr")! as HTMLElement;

describe("ToolLabels", () => {
  it("a built-in label shows its key and has no actions", async () => {
    await stubApi({ "/api/admin/tool-labels": LABELS });
    renderAt("/admin/tools?tab=labels", <ToolLabels />);
    const builtIn = await row("Private data access");
    expect(within(builtIn).getByText("private-data")).toBeInTheDocument();
    expect(within(builtIn).queryByRole("button")).not.toBeInTheDocument();
    // The Actions cell says why it has no action instead of staying blank.
    expect(within(builtIn).getAllByRole("cell").at(-1)).toHaveTextContent("Built-in");
    expect(within(await row("Finance")).getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("delete names the usage count before it deletes", async () => {
    const api = await stubApi({ "/api/admin/tool-labels": LABELS, "DELETE /api/admin/tool-labels/5": null });
    renderAt("/admin/tools?tab=labels", <ToolLabels />);
    fireEvent.click(within(await row("Finance")).getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete this label?" });
    expect(dialog).toHaveTextContent("Finance is removed from 3 tools.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("Label Finance deleted.")).toBeInTheDocument();
    expect(api.calls("DELETE", "/api/admin/tool-labels/5")).toHaveLength(1);
  });

  it("a taken name is refused inline, and the dialog stays open", async () => {
    const api = await stubApi({
      "/api/admin/tool-labels": LABELS,
      "PATCH /api/admin/tool-labels/5": new Response(JSON.stringify({ error: "label exists" }), { status: 409 }),
    });
    renderAt("/admin/tools?tab=labels", <ToolLabels />);
    fireEvent.click(within(await row("Finance")).getByRole("button", { name: "Rename" }));
    const dialog = await screen.findByRole("dialog", { name: "Rename label" });
    expect(dialog).toHaveTextContent("The key stays finance.");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "private data access" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Rename" }));
    expect(await within(dialog).findByText("A label with this name already exists.")).toBeInTheDocument();
    expect(api.calls("PATCH", "/api/admin/tool-labels/5")[0].body).toEqual({ name: "private data access" });
  });
});
