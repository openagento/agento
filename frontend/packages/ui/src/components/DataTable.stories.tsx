import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { DataTable, type Column } from "./DataTable";
import { StatusBadge } from "./StatusBadge";

interface Row { id: string; name: string; state: string }
const rows: Row[] = [{ id: "1", name: "Beta", state: "running" }, { id: "2", name: "Alpha", state: "failed" }];
const columns: Column<Row>[] = [
  { id: "name", header: "Name", cell: (r) => r.name, sortValue: (r) => r.name },
  { id: "state", header: "State", cell: (r) => <StatusBadge tone={r.state === "failed" ? "failed" : "running"}>{r.state}</StatusBadge> },
];

const meta: Meta<typeof DataTable<Row>> = { title: "Components/DataTable", component: DataTable<Row> };
export default meta;
type Story = StoryObj<typeof DataTable<Row>>;
const base = { caption: "Jobs", columns, rowKey: (r: Row) => r.id };

export const WithRows: Story = {
  args: { ...base, rows },
  play: async ({ canvasElement }) => {
    const c = within(canvasElement);
    await userEvent.click(c.getByRole("button", { name: "Name" }));
    const cells = c.getAllByRole("cell").filter((_, i) => i % 2 === 0).map((td) => td.textContent);
    await expect(cells).toEqual(["Alpha", "Beta"]);
  },
};
export const Loading: Story = { args: { ...base, rows: undefined, loading: true } };
export const Empty: Story = { args: { ...base, rows: [], emptyTitle: "No jobs yet" } };
export const Failed: Story = { args: { ...base, rows: undefined, error: "The request failed.", onRetry: () => undefined } };
