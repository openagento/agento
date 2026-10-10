// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { DoubleHeader as DoubleHeaderPattern } from "./DoubleHeader/DoubleHeader";
import { HeaderMegaMenu as HeaderMegaMenuPattern } from "./HeaderMegaMenu/HeaderMegaMenu";
import { HeaderMenu as HeaderMenuPattern } from "./HeaderMenu/HeaderMenu";
import { HeaderSearch as HeaderSearchPattern } from "./HeaderSearch/HeaderSearch";
import { HeaderSimple as HeaderSimplePattern } from "./HeaderSimple/HeaderSimple";
import { HeaderTabs as HeaderTabsPattern } from "./HeaderTabs/HeaderTabs";

const meta: Meta = { title: "Lookbook/Headers", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const DoubleHeader: Story = { name: "Header with multiple layers", render: () => <DoubleHeaderPattern /> };
export const HeaderMegaMenu: Story = { name: "Header with mega menu", render: () => <HeaderMegaMenuPattern /> };
export const HeaderMenu: Story = { name: "Header with menus", render: () => <HeaderMenuPattern /> };
export const HeaderSearch: Story = { name: "Header with search", render: () => <HeaderSearchPattern /> };
export const HeaderSimple: Story = { name: "Simple header", render: () => <HeaderSimplePattern /> };
export const HeaderTabs: Story = { name: "Header with tabs", render: () => <HeaderTabsPattern /> };
