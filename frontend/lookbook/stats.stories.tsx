// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { ProgressCard as ProgressCardPattern } from "./ProgressCard/ProgressCard";
import { ProgressCardColored as ProgressCardColoredPattern } from "./ProgressCardColored/ProgressCardColored";
import { StatsCard as StatsCardPattern } from "./StatsCard/StatsCard";
import { StatsControls as StatsControlsPattern } from "./StatsControls/StatsControls";
import { StatsGrid as StatsGridPattern } from "./StatsGrid/StatsGrid";
import { StatsGridIcons as StatsGridIconsPattern } from "./StatsGridIcons/StatsGridIcons";
import { StatsGroup as StatsGroupPattern } from "./StatsGroup/StatsGroup";
import { StatsRing as StatsRingPattern } from "./StatsRing/StatsRing";
import { StatsSegments as StatsSegmentsPattern } from "./StatsSegments/StatsSegments";

const meta: Meta = { title: "Lookbook/Stats", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const ProgressCard: Story = { name: "Progress card", render: () => <div style={{"maxWidth": 380, "margin": "0 auto"}}><ProgressCardPattern /></div> };
export const ProgressCardColored: Story = { name: "Progress card with color", render: () => <div style={{"maxWidth": 380, "margin": "0 auto"}}><ProgressCardColoredPattern /></div> };
export const StatsCard: Story = { name: "Card with progress", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><StatsCardPattern /></div> };
export const StatsControls: Story = { name: "Stats with controls", render: () => <div style={{"maxWidth": 500, "margin": "0 auto"}}><StatsControlsPattern /></div> };
export const StatsGrid: Story = { name: "Stats grid", render: () => <StatsGridPattern /> };
export const StatsGridIcons: Story = { name: "Stats grid with diff icons", render: () => <StatsGridIconsPattern /> };
export const StatsGroup: Story = { name: "Grouped stats", render: () => <div style={{"maxWidth": 920, "margin": "0 auto"}}><StatsGroupPattern /></div> };
export const StatsRing: Story = { name: "Stats with ring progress", render: () => <div style={{"maxWidth": 840, "margin": "0 auto"}}><StatsRingPattern /></div> };
export const StatsSegments: Story = { name: "Stats with segments", render: () => <div style={{"maxWidth": 440, "margin": "0 auto"}}><StatsSegmentsPattern /></div> };
