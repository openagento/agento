import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { useState } from "react";
import {
  ChatComposer, ChatError, ChatLayout, ChatList, ChatMessage, ChatStatus, Reasoning, RunInfo, ToolCall, ToolGroup,
} from "./Chat";
import { SplitView, ThreadList } from "./ChatShell";

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

export const Status: StoryObj = {
  render: () => (
    <>
      <ChatStatus>Thinking…</ChatStatus>
      <Reasoning text="" live />
      <RunInfo rows={[{ label: "Model", value: "claude-sonnet" }, { label: "Tokens in / out", value: "1200 / 300" }]} />
    </>
  ),
};

function Screen() {
  const [text, setText] = useState("");
  return (
    <SplitView navLabel="Conversations" nav={() => (
      <ThreadList groups={[{ label: "Today", threads: [{ id: 1, title: "List the files", description: "Support", live: true, active: true, onSelect: () => undefined }] }]} />
    )}>
      <ChatLayout title="List the files" actions={<RunInfo rows={[{ label: "Attempt", value: "1 of 3" }]} />}
        footer={<ChatComposer value={text} onChange={setText} onSend={() => setText("")} canSend hint="Enter sends." />}>
        <Turn />
      </ChatLayout>
    </SplitView>
  );
}

export const FullScreen: StoryObj = { render: () => <Screen /> };
