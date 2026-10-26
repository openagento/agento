// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { FeaturesAsymmetrical as FeaturesAsymmetricalPattern } from "./FeaturesAsymmetrical/FeaturesAsymmetrical";
import { FeaturesCards as FeaturesCardsPattern } from "./FeaturesCards/FeaturesCards";
import { FeaturesGrid as FeaturesGridPattern } from "./FeaturesGrid/FeaturesGrid";
import { FeaturesImages as FeaturesImagesPattern } from "./FeaturesImages/FeaturesImages";
import { FeaturesTitle as FeaturesTitlePattern } from "./FeaturesTitle/FeaturesTitle";

const meta: Meta = { title: "Lookbook/Features", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const FeaturesAsymmetrical: Story = { name: "Features with icons", render: () => <FeaturesAsymmetricalPattern /> };
export const FeaturesCards: Story = { name: "Features with cards", render: () => <FeaturesCardsPattern /> };
export const FeaturesGrid: Story = { name: "Features with monotone icons", render: () => <FeaturesGridPattern /> };
export const FeaturesImages: Story = { name: "Features with image icons", render: () => <FeaturesImagesPattern /> };
export const FeaturesTitle: Story = { name: "Features with title", render: () => <div style={{"maxWidth": 1060, "margin": "0 auto"}}><FeaturesTitlePattern /></div> };
