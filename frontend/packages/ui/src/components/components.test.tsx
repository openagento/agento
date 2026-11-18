import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentoUiProvider } from "../Provider";
import { ConfirmDialog } from "./ConfirmDialog";
import { DataTable, type Column } from "./DataTable";
import { Markdown } from "./Markdown";
import { ChatMessage, fileSize, middleEllipsis, ChatComposer, ChatError, ChatLayout, ToolCall } from "./Chat";
import { fileIcon } from "./FileIcon";
import { dayTime } from "./Timestamp";
import { ArchiveAction, DeleteAction, EndAction, MakeCurrentAction, OpenAction, RegenerateAction } from "./IconAction";
import { CopyButton } from "./CopyButton";
import { clampRail, MenuButton, ThreadList } from "./ChatShell";
import { MemoryRouter } from "react-router";
import { AppTile, TileGrid, ViewToggle } from "./Tiles";
import { AppLink } from "./Link";
import { RowMenu } from "./RowMenu";

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

  it("`stoppable` turns the one submit control into Stop; Enter still only sends", () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    render(<ChatComposer value="hi" onChange={vi.fn()} onSend={onSend} canSend={false}
      stoppable onStop={onStop} />, { wrapper: AgentoUiProvider });
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    const box = screen.getByRole("textbox", { name: "Message" });
    expect(box).toBeEnabled();
    // Enter while a turn runs must never stop it: the keyboard path is Send only.
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onStop).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("`stopping` disables Stop, and `onStop` without `stoppable` still renders Send", () => {
    const onStop = vi.fn();
    const { unmount } = render(<ChatComposer value="hi" onChange={vi.fn()} onSend={vi.fn()} canSend={false}
      stoppable onStop={onStop} stopping />, { wrapper: AgentoUiProvider });
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    unmount();
    render(<ChatComposer value="hi" onChange={vi.fn()} onSend={vi.fn()} canSend onStop={onStop} />,
      { wrapper: AgentoUiProvider });
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  it("without `onFiles` the composer has no Add files control at all", () => {
    setup();
    expect(screen.queryByRole("button", { name: "Add files" })).not.toBeInTheDocument();
  });

  it("a picked file reaches onFiles; chips name the files and each one can be removed", () => {
    const onFiles = vi.fn();
    const onRemove = vi.fn();
    render(<ChatComposer value="" onChange={vi.fn()} onSend={vi.fn()} canSend onFiles={onFiles}
      onRemoveAttachment={onRemove}
      attachments={[{ id: "1", name: "report.pdf", status: "ready" },
        { id: "2", name: "notes.txt", status: "uploading" }]} />, { wrapper: AgentoUiProvider });
    expect(screen.getByText("report.pdf")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove notes.txt" }));
    expect(onRemove).toHaveBeenCalledWith("2");
  });

  it("a ready file may be sent with no text; a file still uploading holds Send back", () => {
    const onSend = vi.fn();
    const { unmount } = render(<ChatComposer value="" onChange={vi.fn()} onSend={onSend} canSend
      onFiles={vi.fn()} attachments={[{ id: "1", name: "a.pdf", status: "ready" }]} />,
      { wrapper: AgentoUiProvider });
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
    unmount();
    render(<ChatComposer value="hi" onChange={vi.fn()} onSend={onSend} canSend onFiles={vi.fn()}
      attachments={[{ id: "1", name: "a.pdf", status: "uploading" }]} />, { wrapper: AgentoUiProvider });
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("a failed file keeps its place with the reason, and holds Send until it is removed", () => {
    // Sending around it would silently drop a file the user believes is attached.
    const onRemove = vi.fn();
    render(<ChatComposer value="hi" onChange={vi.fn()} onSend={vi.fn()} canSend onFiles={vi.fn()}
      onRemoveAttachment={onRemove}
      attachments={[{ id: "1", name: "big.zip", status: "error", error: "file too large" }]} />,
      { wrapper: AgentoUiProvider });
    // The reason is READ, not hovered: a title alone tells a touch user and a screen reader
    // nothing about why their file is not going.
    expect(screen.getByText("file too large")).toBeInTheDocument();
    expect(screen.getByTitle("big.zip — file too large")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove big.zip" })).toBeEnabled();
  });

  it("a long name keeps its extension, and the full name stays in the title", () => {
    expect(middleEllipsis("report.pdf")).toBe("report.pdf");
    expect(middleEllipsis("a-very-long-quarterly-report-name.pdf")).toBe("a-very-long-quarter….pdf");
    render(<ChatComposer value="" onChange={vi.fn()} onSend={vi.fn()} canSend onFiles={vi.fn()}
      attachments={[{ id: "1", name: "a-very-long-quarterly-report-name.pdf", status: "ready" }]} />,
      { wrapper: AgentoUiProvider });
    expect(screen.getByTitle("a-very-long-quarterly-report-name.pdf")).toBeInTheDocument();
  });

  it("a sent message shows the files it carried, with no text of its own", () => {
    render(<ChatMessage author="user" text="" attachments={["report.pdf", "notes.txt"]} />,
      { wrapper: AgentoUiProvider });
    expect(screen.getByText("report.pdf")).toBeInTheDocument();
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
  });

});

describe("ChatLayout drop target", () => {
  it("the WHOLE pane takes a drop, and the hint only shows while the drag is over", () => {
    const onFiles = vi.fn();
    render(<ChatLayout title="t" onFiles={onFiles}><p>history</p></ChatLayout>,
      { wrapper: AgentoUiProvider });
    const pane = screen.getByLabelText("Conversation");
    const file = new File(["x"], "a.pdf");
    expect(screen.queryByText("Drop files to attach")).not.toBeInTheDocument();
    fireEvent.dragEnter(pane, { dataTransfer: { files: [file] } });
    expect(screen.getByText("Drop files to attach")).toBeInTheDocument();
    // Leaving a CHILD must not clear the hint - that is what the counter is for.
    const history = screen.getByText("history");
    fireEvent.dragEnter(history, { dataTransfer: { files: [file] } });
    fireEvent.dragLeave(history);
    expect(screen.getByText("Drop files to attach")).toBeInTheDocument();
    // A drop on the HISTORY, far from the composer, is the case the composer-only target missed.
    fireEvent.drop(history, { dataTransfer: { files: [file] } });
    expect(onFiles).toHaveBeenCalledWith([file]);
    expect(screen.queryByText("Drop files to attach")).not.toBeInTheDocument();
  });

  it("takes no drop when the pane is not a target", () => {
    render(<ChatLayout title="t"><p>history</p></ChatLayout>, { wrapper: AgentoUiProvider });
    fireEvent.dragEnter(screen.getByLabelText("Conversation"),
      { dataTransfer: { files: [new File(["x"], "a.pdf")] } });
    expect(screen.queryByText("Drop files to attach")).not.toBeInTheDocument();
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

describe("icon actions (RULES.md UI-7)", () => {
  it("an action is an icon: no text, its name is the accessible name and the hover title", async () => {
    const onClick = vi.fn();
    render(<><ArchiveAction onClick={onClick} /><RegenerateAction onClick={onClick} /><CopyButton value="x" /></>, { wrapper: AgentoUiProvider });
    const archive = screen.getByRole("button", { name: "Archive" });
    expect(archive).toHaveTextContent("");
    expect(screen.getByRole("button", { name: "Copy" })).toHaveTextContent("");
    expect(screen.getByRole("button", { name: "Regenerate" })).toHaveTextContent("");
    fireEvent.mouseEnter(archive);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Archive");
    fireEvent.click(archive);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("the named actions have a default name, a name of their own, and danger where they destroy", () => {
    render(<><OpenAction /><OpenAction label="Preview" /><EndAction /><DeleteAction /><MakeCurrentAction /></>,
      { wrapper: AgentoUiProvider });
    expect(screen.getAllByRole("button").map((b) => b.getAttribute("aria-label")))
      .toEqual(["Open", "Preview", "End", "Delete", "Make current"]);
    expect(screen.getAllByRole("button").every((b) => b.textContent === "")).toBe(true);
    const color = (name: string) => screen.getByRole("button", { name }).getAttribute("style");
    expect(color("Delete")).toBe(color("End"));
    expect(color("Delete")).not.toBe(color("Open"));
  });

  it("one menu holds the run details and the actions", async () => {
    render(<RowMenu label="Conversation actions" actions={[{ label: "Archive", onClick: () => {} }]}
      details={[{ label: "Harness", value: "codex · openai" }, { label: "Credential", value: "team-1" }]} />,
      { wrapper: AgentoUiProvider });
    fireEvent.click(screen.getByRole("button", { name: "Conversation actions" }));
    expect(await screen.findByRole("menuitem", { name: "Archive" })).toBeInTheDocument();
    expect(await screen.findByText("codex · openai")).toBeInTheDocument();
    expect(screen.getByText("team-1")).toBeInTheDocument();
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

describe("tiles", () => {
  it("ViewToggle names itself and reports the picked mode", () => {
    const onChange = vi.fn();
    render(<ViewToggle value="tiles" onChange={onChange} />, { wrapper: AgentoUiProvider });
    expect(screen.getByRole("radiogroup", { name: "Show as" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("List"));
    expect(onChange).toHaveBeenCalledWith("list");
  });

  it("an AppTile opens on click; a disabled one says why and does not open", () => {
    const onOpen = vi.fn();
    render(
      <TileGrid label="Apps">
        <AppTile icon="mail" title="Mail" onOpen={onOpen} />
        <AppTile icon={null} title="Locked" disabled onOpen={onOpen} />
      </TileGrid>, { wrapper: AgentoUiProvider });
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Locked" }));
    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Locked" })).toHaveAttribute("title", "You may view this app but not run it");
    fireEvent.click(screen.getByRole("button", { name: "Mail" }));
    expect(onOpen).toHaveBeenCalledWith(undefined);
  });

  it("an AppTile with several choices is a menu", async () => {
    const onOpen = vi.fn();
    render(<AppTile icon="mail" title="Mail" onOpen={onOpen}
      choices={[{ value: "3", label: "Support" }, { value: "4", label: "Ops" }]} />, { wrapper: AgentoUiProvider });
    fireEvent.click(screen.getByRole("button", { name: "Mail" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Ops" }));
    expect(onOpen).toHaveBeenCalledWith("4");
  });
});

describe("AppLink", () => {
  it("renders a Mantine anchor, so the link takes the theme's anchor color", () => {
    render(<MemoryRouter><AppLink to="/artifacts/demo">demo</AppLink></MemoryRouter>, { wrapper: AgentoUiProvider });
    const a = screen.getByRole("link", { name: "demo" });
    expect(a).toHaveAttribute("href", "/artifacts/demo");
    expect(a.className).toContain("mantine-Anchor-root");
  });
});

describe("RowMenu", () => {
  it("opens from a 3-dots button named Actions and runs the item", async () => {
    const onClick = vi.fn();
    render(<RowMenu actions={[{ label: "Open", onClick }, { label: "Delete", danger: true, onClick: () => {} }]} />,
      { wrapper: AgentoUiProvider });
    fireEvent.click(screen.getByRole("button", { name: "Actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Open" }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("renders nothing when the row has no action", () => {
    render(<RowMenu actions={[]} />, { wrapper: AgentoUiProvider });
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("ThreadList running dot", () => {
  it("marks a running conversation outside the hover-only actions", () => {
    render(<ThreadList groups={[{ label: "Today", threads: [
      { id: 1, title: "Live one", live: true, onSelect: () => {} },
      { id: 2, title: "Quiet one", onSelect: () => {} }] }]} />, { wrapper: AgentoUiProvider });
    const dot = screen.getByLabelText("running");
    expect(dot.className).toContain("statusDot");
    expect(dot.dataset.tone).toBe("running");
    expect(dot.closest("[class*=threadActions]")).toBeNull();
  });
});

describe("clampRail", () => {
  it("keeps the rail width between its bounds", () => {
    expect(clampRail(10)).toBe(220);
    expect(clampRail(9000)).toBe(480);
    expect(clampRail(300.4)).toBe(300);
  });
});

describe("the attachment chip's size", () => {
  it("reads a size at a glance and never outshouts the name", () => {
    expect(fileSize(0)).toBe("0 B");
    expect(fileSize(900)).toBe("900 B");
    expect(fileSize(1024)).toBe("1.0 KB");
    expect(fileSize(15 * 1024)).toBe("15 KB");
    expect(fileSize(2.5 * 1024 * 1024)).toBe("2.5 MB");
    render(<ChatComposer value="" onChange={() => {}} onSend={() => {}} canSend
      attachments={[{ id: "a", name: "raport.pdf", status: "ready", bytes: 2048 }]}
      onFiles={() => {}} onRemoveAttachment={() => {}} />, { wrapper: AgentoUiProvider });
    expect(screen.getByText("raport.pdf")).toBeInTheDocument();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
  });
});

describe("the chip's icon", () => {
  it("names the family of the file, and falls back to the paperclip", () => {
    const kind = (name: string) => {
      const { container } = render(<>{fileIcon(name)}</>);
      return container.querySelector("svg")?.getAttribute("class") ?? "";
    };
    // Same family, same icon; a different family, a different one.
    expect(kind("shot.png")).toBe(kind("photo.jpeg"));
    expect(kind("budget.xlsx")).toBe(kind("rows.csv"));
    expect(kind("shot.png")).not.toBe(kind("budget.xlsx"));
    expect(kind("index.html")).not.toBe(kind("main.py"));
    // Unknown and extension-less names both keep the paperclip the chip always had.
    expect(kind("archive.zzz")).toBe(kind("Makefile"));
    expect(kind("archive.zzz")).toContain("paperclip");
  });
});

describe("a turn's clock line", () => {
  const now = new Date("2026-11-18T12:00:00Z");
  const fmt = { dateFormat: "iso" as const, timeZone: "UTC" };
  it("says the day in words while the day is near, then the date", () => {
    expect(dayTime(new Date("2026-11-18T10:26:00Z"), fmt, now)).toBe("Today 10:26");
    expect(dayTime(new Date("2026-11-17T23:59:00Z"), fmt, now)).toBe("Yesterday 23:59");
    expect(dayTime(new Date("2026-11-16T08:05:00Z"), fmt, now)).toBe("2026-11-16 08:05");
    // The day is decided in the label's OWN zone, not the browser's.
    expect(dayTime(new Date("2026-11-18T00:30:00Z"), { dateFormat: "us", timeZone: "America/New_York" }, now))
      .toBe("Yesterday 7:30 PM");
  });
});

describe("a user's message", () => {
  it("carries its files above the bubble, not inside it", () => {
    const { container } = render(
      <ChatMessage author="user" text="podsumuj" attachments={["index.html"]} />,
      { wrapper: AgentoUiProvider });
    const chip = screen.getByText("index.html");
    const bubble = container.querySelector("[class*='bubble']");
    expect(bubble).not.toBeNull();
    expect(bubble!.contains(chip)).toBe(false);
    // The chips come first, as the composer stacked them above the text line.
    expect(chip.compareDocumentPosition(bubble!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
