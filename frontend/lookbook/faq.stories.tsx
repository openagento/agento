// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { FaqSimple as FaqSimplePattern } from "./FaqSimple/FaqSimple";
import { FaqWithBg as FaqWithBgPattern } from "./FaqWithBg/FaqWithBg";
import { FaqWithHeader as FaqWithHeaderPattern } from "./FaqWithHeader/FaqWithHeader";
import { FaqWithImage as FaqWithImagePattern } from "./FaqWithImage/FaqWithImage";

const meta: Meta = { title: "Lookbook/FAQ", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const FaqSimple: Story = { name: "FAQ simple", render: () => <FaqSimplePattern /> };
export const FaqWithBg: Story = { name: "Faq with background", render: () => <FaqWithBgPattern /> };
export const FaqWithHeader: Story = { name: "Faq page header", render: () => <FaqWithHeaderPattern /> };
export const FaqWithImage: Story = { name: "Faq with image", render: () => <FaqWithImagePattern /> };
