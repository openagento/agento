// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { GridAsymmetrical as GridAsymmetricalPattern } from "./GridAsymmetrical/GridAsymmetrical";
import { LeadGrid as LeadGridPattern } from "./LeadGrid/LeadGrid";
import { Subgrid as SubgridPattern } from "./Subgrid/Subgrid";

const meta: Meta = { title: "Lookbook/Grids", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const GridAsymmetrical: Story = { name: "Grid with asymmetrical columns", render: () => <GridAsymmetricalPattern /> };
export const LeadGrid: Story = { name: "Grid with leading item", render: () => <LeadGridPattern /> };
export const Subgrid: Story = { name: "Grid with vertical items", render: () => <SubgridPattern /> };
