// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { FooterCentered as FooterCenteredPattern } from "./FooterCentered/FooterCentered";
import { FooterLinks as FooterLinksPattern } from "./FooterLinks/FooterLinks";
import { FooterSimple as FooterSimplePattern } from "./FooterSimple/FooterSimple";
import { FooterSocial as FooterSocialPattern } from "./FooterSocial/FooterSocial";

const meta: Meta = { title: "Lookbook/Footers", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const FooterCentered: Story = { name: "Footer with centered links", render: () => <FooterCenteredPattern /> };
export const FooterLinks: Story = { name: "Footer with links", render: () => <FooterLinksPattern /> };
export const FooterSimple: Story = { name: "Simple footer", render: () => <FooterSimplePattern /> };
export const FooterSocial: Story = { name: "Footer with social icons", render: () => <FooterSocialPattern /> };
