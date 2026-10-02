// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { ActionToggle as ActionTogglePattern } from "./ActionToggle/ActionToggle";
import { ButtonCopy as ButtonCopyPattern } from "./ButtonCopy/ButtonCopy";
import { ButtonMenu as ButtonMenuPattern } from "./ButtonMenu/ButtonMenu";
import { ButtonProgress as ButtonProgressPattern } from "./ButtonProgress/ButtonProgress";
import { SocialButtons as SocialButtonsPattern } from "./SocialButtons/SocialButtons";
import { SplitButton as SplitButtonPattern } from "./SplitButton/SplitButton";

const meta: Meta = { title: "Lookbook/Buttons", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const ActionToggle: Story = { name: "Color scheme toggle", render: () => <div style={{"margin": "0 auto"}}><ActionTogglePattern /></div> };
export const ButtonCopy: Story = { name: "Copy to clipboard button", render: () => <div style={{"maxWidth": 240, "margin": "0 auto"}}><ButtonCopyPattern /></div> };
export const ButtonMenu: Story = { name: "Button with menu", render: () => <div style={{"maxWidth": 120, "margin": "0 auto"}}><ButtonMenuPattern /></div> };
export const ButtonProgress: Story = { name: "Button with loading progress", render: () => <div style={{"maxWidth": 220, "margin": "0 auto"}}><ButtonProgressPattern /></div> };
export const SocialButtons: Story = { name: "Social buttons", render: () => <div style={{"margin": "0 auto"}}><SocialButtonsPattern /></div> };
export const SplitButton: Story = { name: "Split button", render: () => <div style={{"maxWidth": 120, "margin": "0 auto"}}><SplitButtonPattern /></div> };
