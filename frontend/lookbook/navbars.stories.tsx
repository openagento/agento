// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { DoubleNavbar as DoubleNavbarPattern } from "./DoubleNavbar/DoubleNavbar";
import { NavbarLinksGroup as NavbarLinksGroupPattern } from "./NavbarLinksGroup/NavbarLinksGroup";
import { NavbarMinimal as NavbarMinimalPattern } from "./NavbarMinimal/NavbarMinimal";
import { NavbarMinimalColored as NavbarMinimalColoredPattern } from "./NavbarMinimalColored/NavbarMinimalColored";
import { NavbarNested as NavbarNestedPattern } from "./NavbarNested/NavbarNested";
import { NavbarSearch as NavbarSearchPattern } from "./NavbarSearch/NavbarSearch";
import { NavbarSegmented as NavbarSegmentedPattern } from "./NavbarSegmented/NavbarSegmented";
import { NavbarSimple as NavbarSimplePattern } from "./NavbarSimple/NavbarSimple";
import { NavbarSimpleColored as NavbarSimpleColoredPattern } from "./NavbarSimpleColored/NavbarSimpleColored";

const meta: Meta = { title: "Lookbook/Navbars", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const DoubleNavbar: Story = { name: "Navbar with 2 sections", render: () => <DoubleNavbarPattern /> };
export const NavbarLinksGroup: Story = { name: "Collapsible links group", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><NavbarLinksGroupPattern /></div> };
export const NavbarMinimal: Story = { name: "Navbar with tooltips", render: () => <NavbarMinimalPattern /> };
export const NavbarMinimalColored: Story = { name: "Navbar with tooltips", render: () => <NavbarMinimalColoredPattern /> };
export const NavbarNested: Story = { name: "Navbar with nested links", render: () => <NavbarNestedPattern /> };
export const NavbarSearch: Story = { name: "Navbar with search", render: () => <NavbarSearchPattern /> };
export const NavbarSegmented: Story = { name: "Navbar with SegmentedControl", render: () => <NavbarSegmentedPattern /> };
export const NavbarSimple: Story = { name: "Simple navbar", render: () => <NavbarSimplePattern /> };
export const NavbarSimpleColored: Story = { name: "Simple navbar", render: () => <NavbarSimpleColoredPattern /> };
