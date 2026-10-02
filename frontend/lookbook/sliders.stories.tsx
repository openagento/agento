// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { SliderHover as SliderHoverPattern } from "./SliderHover/SliderHover";
import { SliderIcon as SliderIconPattern } from "./SliderIcon/SliderIcon";
import { SliderInput as SliderInputPattern } from "./SliderInput/SliderInput";
import { SliderLabel as SliderLabelPattern } from "./SliderLabel/SliderLabel";
import { SliderMarks as SliderMarksPattern } from "./SliderMarks/SliderMarks";
import { SliderWhite as SliderWhitePattern } from "./SliderWhite/SliderWhite";

const meta: Meta = { title: "Lookbook/Sliders", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const SliderHover: Story = { name: "Slider with thumb visible on hover", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><SliderHoverPattern /></div> };
export const SliderIcon: Story = { name: "Slider with icon thumb", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><SliderIconPattern /></div> };
export const SliderInput: Story = { name: "NumberInput with slider", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><SliderInputPattern /></div> };
export const SliderLabel: Story = { name: "Range slider with labels", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><SliderLabelPattern /></div> };
export const SliderMarks: Story = { name: "Slider with marks", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><SliderMarksPattern /></div> };
export const SliderWhite: Story = { name: "Slider with white thumb", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><SliderWhitePattern /></div> };
