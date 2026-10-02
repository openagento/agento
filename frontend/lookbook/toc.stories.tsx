// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { TableOfContents as TableOfContentsPattern } from "./TableOfContents/TableOfContents";
import { TableOfContentsFloating as TableOfContentsFloatingPattern } from "./TableOfContentsFloating/TableOfContentsFloating";

const meta: Meta = { title: "Lookbook/Table of contents", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const TableOfContents: Story = { name: "Table of contents", render: () => <div style={{"maxWidth": 280, "margin": "0 auto"}}><TableOfContentsPattern /></div> };
export const TableOfContentsFloating: Story = { name: "Table of contents indicator", render: () => <div style={{"maxWidth": 280, "margin": "0 auto"}}><TableOfContentsFloatingPattern /></div> };
