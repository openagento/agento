import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentoUiProvider } from "../Provider";
import { ConfirmDialog } from "./ConfirmDialog";
import { DataTable, type Column } from "./DataTable";
import { Markdown } from "./Markdown";
import { ChatComposer, ChatError, ToolCall } from "./Chat";
import { MenuButton, ThreadList } from "./ChatShell";

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

describe("Markdown highlight", () => {
  it("opt-in: a fenced block gets its language label and a copy button; the text stays", async () => {
    const { container } = render(<Markdown highlight>{"```python\nprint(1)\n```"}</Markdown>, { wrapper: AgentoUiProvider });
    expect(await screen.findByRole("button", { name: "Copy code" }, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("python")).toBeInTheDocument();
    expect(container.textContent).toContain("print(1)");
  }, 15_000);
});

describe("ChatComposer", () => {
  function setup(canSend = true) {
    const onSend = vi.fn();
    const onChange = vi.fn();
    render(<ChatComposer value="hi" onChange={onChange} onSend={onSend} canSend={canSend} />, { wrapper: AgentoUiProvider });
    return { onSend, onChange, box: screen.getByRole("textbox", { name: "Message" }) };
  }

  it("Enter sends; Shift+Enter and an IME composition do not", () => {
    const { onSend, box } = setup();
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("while it cannot send, typing works and Send is disabled", () => {
    const { onSend, onChange, box } = setup(false);
    expect(box).toBeEnabled();
    fireEvent.change(box, { target: { value: "more" } });
    expect(onChange).toHaveBeenCalledWith("more");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("ToolCall and ChatError", () => {
  it("a tool row opens its input and output; with nothing to show it does not open", () => {
    render(<><ToolCall summary="Ran ls" running={false} input="ls" output="a" /><ToolCall summary="Read a file" running={false} /></>,
      { wrapper: AgentoUiProvider });
    const row = screen.getByRole("button", { name: /Ran ls/ });
    expect(row).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(row);
    expect(row).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Read a file/ })).toBeDisabled();
  });

  it("Retry calls back", () => {
    const onRetry = vi.fn();
    render(<ChatError title="The agent could not answer." details="raw" onRetry={onRetry} />, { wrapper: AgentoUiProvider });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("ThreadList and MenuButton", () => {
  it("shows day groups that have threads; the open thread is aria-current", () => {
    const onSelect = vi.fn();
    render(<ThreadList groups={[
      { label: "Today", threads: [{ id: 1, title: "First", active: true, onSelect }, { id: 2, title: "Second", onSelect }] },
      { label: "Older", threads: [] },
    ]} />, { wrapper: AgentoUiProvider });
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.queryByText("Older")).toBeNull();
    expect(screen.getByRole("button", { name: "First" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(screen.getByRole("button", { name: "Second" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("one choice is a plain button; more open a menu", async () => {
    const onSelect = vi.fn();
    const { rerender } = render(<MenuButton label="New conversation" items={[{ value: "1", label: "Support" }]} onSelect={onSelect} />,
      { wrapper: AgentoUiProvider });
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    expect(onSelect).toHaveBeenLastCalledWith("1");
    rerender(<MenuButton label="New conversation" items={[{ value: "1", label: "Support" }, { value: "2", label: "Sales" }]} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Sales" }));
    expect(onSelect).toHaveBeenLastCalledWith("2");
  });
});
