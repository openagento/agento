import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { useState } from "react";
import {
  ChatComposer, ChatError, ChatLayout, ChatList, ChatMessage, ChatStatus, Reasoning, SubagentRun, ToolCall, ToolGroup,
} from "./Chat";
import { MenuButton, Rail, SplitView, ThreadList } from "./ChatShell";
import { RowMenu } from "./RowMenu";

const meta: Meta = { title: "Components/Chat" };
export default meta;

const ANSWER = "## Files\n\n| Name |\n|---|\n| README.md |\n\n```python\nprint('hello')\n```";

function Turn() {
  return (
    <ChatList>
      <li><ChatMessage author="user" text="List the files, please." time="2026-10-06T09:00:00Z" /></li>
      <li><Reasoning text="The user wants a list. I will run ls." live={false} seconds={2} /></li>
      <li>
        <ToolGroup count={2} running={false}>
          <ToolCall summary="Ran ls -1" running={false} duration="0.4 s" input="ls -1" output={"README.md\nsrc"} />
          <ToolCall summary="Read README.md" running={false} duration="0.1 s" />
        </ToolGroup>
      </li>
      <li><ToolCall summary="Running git status" running /></li>
      <li><ChatMessage author="assistant" text={ANSWER} /></li>
      <li><ChatError title="The agent could not answer." details="rate limited (3 errors)" onRetry={() => undefined}>The run failed after 3 of 3 attempts.</ChatError></li>
    </ChatList>
  );
}

export const Messages: StoryObj = {
  render: () => (
    <ChatList>
      <li><ChatMessage author="incoming" label="From jira · DEMO-1" text="DEMO-1 is due tomorrow." /></li>
      <li><ChatMessage author="user" text={"Hello\nsecond line"} /></li>
      <li><ChatMessage author="assistant" text="**Hi.** How can I help?" /></li>
    </ChatList>
  ),
};

export const ToolsAndErrors: StoryObj = {
  render: () => <Turn />,
  play: async ({ canvasElement }) => {
    const c = within(canvasElement);
    const row = c.getByRole("button", { name: /Used 2 tools/ });
    await userEvent.click(row);
    await expect(row).toHaveAttribute("aria-expanded", "true");
  },
};

export const Subagents: StoryObj = {
  render: () => (
    <ChatList>
      <li><ToolCall summary="agent_run" running={false} duration="0.2 s" /></li>
      <li>
        <SubagentRun label="Sub-agent #12 · reviewer · claude" state="succeeded">
          <ChatList>
            <li><ToolCall summary="Read src/app.py" running={false} duration="0.1 s" /></li>
            <li><ChatMessage author="assistant" text="Two findings." /></li>
          </ChatList>
        </SubagentRun>
      </li>
      <li><SubagentRun label="Sub-agent #13 · tester" state="running" /></li>
    </ChatList>
  ),
  play: async ({ canvasElement }) => {
    const row = within(canvasElement).getByRole("button", { name: /Sub-agent #12/ });
    await expect(row).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(row);
    await expect(row).toHaveAttribute("aria-expanded", "true");
  },
};

export const Status: StoryObj = {
  render: () => (
    <>
      <ChatStatus>Thinking…</ChatStatus>
      <Reasoning text="" live />
    </>
  ),
};

function Screen() {
  const [text, setText] = useState("");
  return (
    <SplitView navLabel="Conversations" nav={() => (
      <Rail top={<MenuButton label="New conversation" icon="new" variant="subtle" items={[{ value: "a", label: "Support" }]} onSelect={() => undefined} />}>
        <ThreadList groups={[{ label: "Today", threads: [{ id: 1, title: "List the files", description: "Support", live: true, active: true, onSelect: () => undefined,
          children: [{ id: "job-2", title: "Sub-agent #2", status: "succeeded", onSelect: () => undefined,
            children: [{ id: "job-3", title: "Sub-agent #3", status: "running", onSelect: () => undefined },
              { id: "job-4", title: "Sub-agent #4", status: "pending", onSelect: () => undefined }] }] },
          // Long names and enough rows to scroll: the rail truncates and keeps one scrollbar.
          ...Array.from({ length: 20 }, (_, i) => ({ id: 10 + i, title: `outlook re-sprzedaz-zpp::AAMkAGI2NDQ1MzY3 #${i}`, onSelect: () => undefined })),
        ] }]} />
      </Rail>
    )}>
      <ChatLayout title="List the files" actions={<RowMenu label="Conversation actions" details={[{ label: "Attempt", value: "1 of 3" }]} actions={[]} />}
        footer={<ChatComposer value={text} onChange={setText} onSend={() => setText("")} canSend hint="Enter sends." />}>
        <Turn />
      </ChatLayout>
    </SplitView>
  );
}

export const FullScreen: StoryObj = { render: () => <Screen /> };

// Content that cannot wrap: a 12-column table, a 300-char word (in a paragraph, in inline code and
// in a URL) and a fenced line of 300 chars. The column must stay centred and nothing may overflow it.
const WORD = "x".repeat(300);
const HEAD = Array.from({ length: 12 }, (_, i) => `Column header ${String(i + 1).padStart(2, "0")}`);
const WIDE_TABLE = [`| ${HEAD.join(" | ")} |`, `|${"---|".repeat(12)}`,
  ...[1, 2, 3].map((r) => `| ${HEAD.map((_, c) => `r${r}c${c + 1}`).join(" | ")} |`)].join("\n");
const WIDE_TEXT = `${WORD}\n\nInline \`${WORD}\` and https://example.com/${WORD}`;
const WIDE_CODE = "```text\n" + WORD + "\n```";

export const WideContent: StoryObj = {
  render: () => (
    <div style={{ width: 900, height: 600 }}>
      <ChatLayout title="Wide content">
        <ChatList>
          <li><ChatMessage author="user" text="Show me the report." /></li>
          <li><ChatMessage author="assistant" text={WIDE_TABLE} /></li>
          <li><ChatMessage author="assistant" text={WIDE_TEXT} /></li>
          <li><ChatMessage author="assistant" text={WIDE_CODE} /></li>
        </ChatList>
      </ChatLayout>
    </div>
  ),
  play: async ({ canvasElement }) => {
    const c = within(canvasElement);
    const table = await c.findByRole("table");
    // HighlightedCode is a lazy chunk: its language label shows once it has loaded.
    const code = (await c.findByText("text")).parentElement!;
    const viewport = c.getByRole("region", { name: "Timeline" });
    const column = viewport.firstElementChild!.firstElementChild as HTMLElement;
    const vp = viewport.getBoundingClientRect();
    const col = column.getBoundingClientRect();
    // No horizontal overflow in the timeline, and the column is centred in it.
    await expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
    await expect(Math.abs((col.left - vp.left) - (vp.left + viewport.clientWidth - col.right))).toBeLessThanOrEqual(1);
    // The table scrolls in its own box; the text and the code stay inside the column.
    const box = c.getByRole("region", { name: "Table" });
    await expect(box).toContainElement(table);
    await expect(box.scrollWidth).toBeGreaterThan(box.clientWidth);
    const inside = (el: Element) => el.getBoundingClientRect().right <= col.right + 1;
    await expect(inside(box)).toBe(true);
    for (const p of column.querySelectorAll("p")) await expect(inside(p)).toBe(true);
    await expect(inside(code)).toBe(true);
  },
};
