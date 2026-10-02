// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { DndList as DndListPattern } from "./DndList/DndList";
import { DndListHandle as DndListHandlePattern } from "./DndListHandle/DndListHandle";
import { DndTable as DndTablePattern } from "./DndTable/DndTable";

const meta: Meta = { title: "Lookbook/Drag and drop", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const DndList: Story = { name: "Drag'n'drop list", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><DndListPattern /></div> };
export const DndListHandle: Story = { name: "Drag'n'drop list with handle", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><DndListHandlePattern /></div> };
export const DndTable: Story = { name: "Drag'n'drop table", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><DndTablePattern /></div> };
