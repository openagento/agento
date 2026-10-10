import type { Meta, StoryObj } from "@storybook/react-vite";
import { Button } from "./Button";
import { StatusBadge } from "./StatusBadge";
import { Card } from "./Card";
import { PageHeader, SectionHeader } from "./Headers";
import { Timestamp } from "./Timestamp";
import { CopyButton } from "./CopyButton";
import { CodeBlock, JsonViewer } from "./CodeBlock";
import { ConnectionStatus } from "./ConnectionStatus";

const meta: Meta = { title: "Components/Basics" };
export default meta;
type Story = StoryObj;

export const Buttons: Story = {
  render: () => (
    <div className="ag-row">
      <Button>Default</Button><Button variant="primary">Primary</Button>
      <Button variant="subtle">Subtle</Button><Button variant="danger">Danger</Button>
      <Button disabled>Disabled</Button>
    </div>
  ),
};

export const Badges: Story = {
  render: () => (
    <div className="ag-row">
      {(["pending", "published", "running", "succeeded", "failed", "blocked", "neutral", "info"] as const).map((t) => (
        <StatusBadge key={t} tone={t}>{t}</StatusBadge>
      ))}
    </div>
  ),
};

export const Cards: Story = {
  render: () => (
    <div className="ag-stack">
      <Card title="With title" meta="Updated just now">Body text.</Card>
      <Card title="With actions" actions={<Button variant="subtle">Edit</Button>}>Body text.</Card>
    </div>
  ),
};

export const Headers: Story = {
  render: () => (
    <>
      <PageHeader title="Users" description="Who can sign in." actions={<Button variant="primary">Add</Button>} />
      <SectionHeader>Grants</SectionHeader>
    </>
  ),
};

export const TimestampCopyCode: Story = {
  render: () => (
    <div className="ag-stack">
      <p>Created <Timestamp value="2026-10-01T10:42:00Z" />; bad value: <Timestamp value="not a date" /></p>
      <div className="ag-row"><code>job-123</code><CopyButton value="job-123" /></div>
      <CodeBlock code={"agento config:set core/x 1"} copy />
      <JsonViewer value={{ ok: true, items: [1, 2, 3] }} />
    </div>
  ),
};

export const Connection: Story = {
  render: () => (
    <div className="ag-stack">
      <ConnectionStatus state="connecting" />
      <ConnectionStatus state="live" />
      <ConnectionStatus state="reconnecting" />
      <ConnectionStatus state="refused" onReconnect={() => undefined} />
    </div>
  ),
};
