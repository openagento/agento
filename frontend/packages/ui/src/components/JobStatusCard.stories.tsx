import type { Meta, StoryObj } from "@storybook/react-vite";
import { JobStatusCard } from "./JobStatusCard";

const meta: Meta<typeof JobStatusCard> = { title: "Components/JobStatusCard", component: JobStatusCard };
export default meta;
type Story = StoryObj<typeof JobStatusCard>;

export const Pending: Story = { args: { title: "Nightly import", state: "pending" } };
export const Queued: Story = { args: { title: "Nightly import", state: "published" } };
export const Running: Story = { args: { title: "Nightly import", state: "running", detail: "Step 2 of 4" } };
export const Succeeded: Story = { args: { title: "Nightly import", state: "succeeded", updatedAt: "2026-10-01T10:42:00Z" } };
export const Failed: Story = { args: { title: "Nightly import", state: "failed", detail: "The toolbox timed out." } };
export const Blocked: Story = { args: { title: "Nightly import", state: "blocked", detail: "This turn cannot resume by itself." } };
