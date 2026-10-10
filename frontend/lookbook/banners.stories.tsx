// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { CookiesBanner as CookiesBannerPattern } from "./CookiesBanner/CookiesBanner";
import { EmailBanner as EmailBannerPattern } from "./EmailBanner/EmailBanner";
import { ImageActionBanner as ImageActionBannerPattern } from "./ImageActionBanner/ImageActionBanner";

const meta: Meta = { title: "Lookbook/Banners", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const CookiesBanner: Story = { name: "Cookies banner", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><CookiesBannerPattern /></div> };
export const EmailBanner: Story = { name: "Email banner", render: () => <div style={{"maxWidth": 980, "margin": "0 auto"}}><EmailBannerPattern /></div> };
export const ImageActionBanner: Story = { name: "Banner with image and action", render: () => <div style={{"maxWidth": 480, "margin": "0 auto"}}><ImageActionBannerPattern /></div> };
