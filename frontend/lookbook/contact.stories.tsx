// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { ContactUs as ContactUsPattern } from "./ContactUs/ContactUs";
import { GetInTouch as GetInTouchPattern } from "./GetInTouch/GetInTouch";
import { GetInTouchSimple as GetInTouchSimplePattern } from "./GetInTouchSimple/GetInTouchSimple";

const meta: Meta = { title: "Lookbook/Contact", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const ContactUs: Story = { name: "Contact us form", render: () => <div style={{"maxWidth": 1000, "margin": "0 auto"}}><ContactUsPattern /></div> };
export const GetInTouch: Story = { name: "Get in touch form", render: () => <div style={{"maxWidth": 800, "margin": "0 auto"}}><GetInTouchPattern /></div> };
export const GetInTouchSimple: Story = { name: "Get in touch form", render: () => <div style={{"maxWidth": 620, "margin": "0 auto"}}><GetInTouchSimplePattern /></div> };
