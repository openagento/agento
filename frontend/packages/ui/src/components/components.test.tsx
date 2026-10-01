import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { agentoTheme } from "../theme";
import { ConfirmDialog } from "./ConfirmDialog";
import { DataTable, type Column } from "./DataTable";

interface Row { id: string; name: string }
const rows: Row[] = [{ id: "a", name: "Alpha" }, { id: "b", name: "Beta" }, { id: "c", name: "Gamma" }];
const columns: Column<Row>[] = [{ id: "name", header: "Name", cell: (r) => r.name, sortValue: (r) => r.name }];

describe("DataTable keyboard", () => {
  it("one row is in the tab order; arrows, Home and End move focus; Enter activates", () => {
    const onRowActivate = vi.fn();
    render(<DataTable caption="Rows" columns={columns} rows={rows} rowKey={(r) => r.id} onRowActivate={onRowActivate} />);
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
});

describe("ConfirmDialog", () => {
  it("Escape cancels and the confirm button confirms", async () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(
      <MantineProvider theme={agentoTheme}>
        <ConfirmDialog opened title="Delete user?" onConfirm={onConfirm} onCancel={onCancel} confirmLabel="Delete" />
      </MantineProvider>,
    );
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAccessibleName("Delete user?");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

