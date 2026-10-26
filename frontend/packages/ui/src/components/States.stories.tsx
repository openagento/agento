import type { Meta, StoryObj } from "@storybook/react-vite";
import { EmptyState, ErrorState, LoadingState } from "./States";

const meta: Meta = { title: "Components/States" };
export default meta;

export const Empty: StoryObj = { render: () => <EmptyState title="No jobs yet">A job shows here when it starts.</EmptyState> };
export const Loading: StoryObj = { render: () => <LoadingState /> };
export const Error: StoryObj = { render: () => <ErrorState message="The toolbox is not available." onRetry={() => undefined} /> };
