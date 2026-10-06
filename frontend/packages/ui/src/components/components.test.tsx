import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentoUiProvider } from "../Provider";
import { ConfirmDialog } from "./ConfirmDialog";
import { DataTable, type Column } from "./DataTable";
import { Markdown } from "./Markdown";

interface Row { id: string; name: string }
const rows: Row[] = [{ id: "a", name: "Alpha" }, { id: "b", name: "Beta" }, { id: "c", name: "Gamma" }];
const columns: Column<Row>[] = [{ id: "name", header: "Name", cell: (r) => r.name, sortValue: (r) => r.name }];

describe("DataTable keyboard", () => {
  it("one row is in the tab order; arrows, Home and End move focus; Enter activates", () => {
    const onRowActivate = vi.fn();
    render(<DataTable caption="Rows" columns={columns} rows={rows} rowKey={(r) => r.id} onRowActivate={onRowActivate} />,
      { wrapper: AgentoUiProvider });
    const body = screen.getAllByRole("row").slice(1);
    expect(body.map((r) => r.tabIndex)).toEqual([0, -1, -1]);
    body[0].focus();
    fireEvent.keyDown(body[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(body[1]);
    fireEvent.keyDown(body[1], { key: "End" });
    expect(document.activeElement).toBe(body[2]);
    fireEvent.keyDown(body[2], { key: "Home" });
    expect(document.activeElement).toBe(body[0]);
    fireEvent.keyDown(body[0], { key: "ArrowUp" });
    expect(document.activeElement).toBe(body[0]);
    fireEvent.keyDown(body[0], { key: "Enter" });
    expect(onRowActivate).toHaveBeenCalledWith(rows[0]);
  });

  it("leaves the keys of a control in a cell to that control", () => {
    const onRowActivate = vi.fn();
    const withButton: Column<Row>[] = [...columns, { id: "act", header: "Actions", cell: (r) => <button type="button">Edit {r.name}</button> }];
    render(<DataTable caption="Rows" columns={withButton} rows={rows} rowKey={(r) => r.id} onRowActivate={onRowActivate} />,
      { wrapper: AgentoUiProvider });
    const button = screen.getByRole("button", { name: "Edit Alpha" });
    button.focus();
    fireEvent.keyDown(button, { key: "ArrowDown" });
    fireEvent.keyDown(button, { key: "Enter" });
    expect(document.activeElement).toBe(button);
    expect(onRowActivate).not.toHaveBeenCalled();
  });

  it("keeps a cell mounted when a row takes focus, so a cell control keeps its state", () => {
    const withInput: Column<Row>[] = [...columns, { id: "note", header: "Note", cell: (r) => <input aria-label={`Note ${r.name}`} /> }];
    render(<DataTable caption="Rows" columns={withInput} rows={rows} rowKey={(r) => r.id} />, { wrapper: AgentoUiProvider });
    const input = screen.getByRole("textbox", { name: "Note Beta" });
    fireEvent.focus(input);
    expect(screen.getByRole("textbox", { name: "Note Beta" })).toBe(input);
  });
});

describe("ConfirmDialog", () => {
  it("Escape cancels and the confirm button confirms", async () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(
      <AgentoUiProvider>
        <ConfirmDialog opened title="Delete user?" onConfirm={onConfirm} onCancel={onCancel} confirmLabel="Delete" />
      </AgentoUiProvider>,
    );
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAccessibleName("Delete user?");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});


describe("Markdown", () => {
  it("renders a code fence, a GFM table and a safe link; raw HTML stays text", async () => {
    const md = "```js\nconst a = 1;\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n[site](https://example.com)\n\n<script>window.pwned = 1</script>\n\n<b>bold</b>";
    const { container } = render(<Markdown>{md}</Markdown>, { wrapper: AgentoUiProvider });
    // The renderer is a lazy chunk; a cold transform under a full run can take seconds.
    expect(await screen.findByRole("table", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(container.querySelector("pre code")!.textContent).toBe("const a = 1;\n");
    expect(screen.getByRole("link", { name: "site" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>window.pwned = 1</script>");
    expect(container.querySelector("b")).toBeNull();
    expect((window as { pwned?: number }).pwned).toBeUndefined();
  }, 15_000);
});
