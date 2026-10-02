// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { DropzoneButton as DropzoneButtonPattern } from "./DropzoneButton/DropzoneButton";

const meta: Meta = { title: "Lookbook/Dropzones", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const DropzoneButton: Story = { name: "Dropzone with button", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><DropzoneButtonPattern /></div> };
