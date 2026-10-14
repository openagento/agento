// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { TableReviews as TableReviewsPattern } from "./TableReviews/TableReviews";
import { TableScrollArea as TableScrollAreaPattern } from "./TableScrollArea/TableScrollArea";
import { TableSelection as TableSelectionPattern } from "./TableSelection/TableSelection";
import { TableSort as TableSortPattern } from "./TableSort/TableSort";

const meta: Meta = { title: "Lookbook/Tables", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const TableReviews: Story = { name: "Table with progress", render: () => <div style={{"maxWidth": 920, "margin": "0 auto"}}><TableReviewsPattern /></div> };
export const TableScrollArea: Story = { name: "Table with sticky header", render: () => <div style={{"maxWidth": 920, "margin": "0 auto"}}><TableScrollAreaPattern /></div> };
export const TableSelection: Story = { name: "Table with selection", render: () => <div style={{"maxWidth": 800, "margin": "0 auto"}}><TableSelectionPattern /></div> };
export const TableSort: Story = { name: "Table with search and sort", render: () => <div style={{"maxWidth": 920, "margin": "0 auto"}}><TableSortPattern /></div> };
