// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { HeroBullets as HeroBulletsPattern } from "./HeroBullets/HeroBullets";
import { HeroContentLeft as HeroContentLeftPattern } from "./HeroContentLeft/HeroContentLeft";
import { HeroImageBackground as HeroImageBackgroundPattern } from "./HeroImageBackground/HeroImageBackground";
import { HeroImageRight as HeroImageRightPattern } from "./HeroImageRight/HeroImageRight";
import { HeroText as HeroTextPattern } from "./HeroText/HeroText";
import { HeroTitle as HeroTitlePattern } from "./HeroTitle/HeroTitle";

const meta: Meta = { title: "Lookbook/Hero", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const HeroBullets: Story = { name: "Hero with bullets", render: () => <HeroBulletsPattern /> };
export const HeroContentLeft: Story = { name: "Hero with content on left", render: () => <HeroContentLeftPattern /> };
export const HeroImageBackground: Story = { name: "Hero with background image", render: () => <HeroImageBackgroundPattern /> };
export const HeroImageRight: Story = { name: "Hero with image on the right", render: () => <HeroImageRightPattern /> };
export const HeroText: Story = { name: "Hero section with text", render: () => <HeroTextPattern /> };
export const HeroTitle: Story = { name: "Hero section with gradient text", render: () => <HeroTitlePattern /> };
