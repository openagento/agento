// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { ActionsGrid as ActionsGridPattern } from "./ActionsGrid/ActionsGrid";
import { BadgeCard as BadgeCardPattern } from "./BadgeCard/BadgeCard";
import { CardWithStats as CardWithStatsPattern } from "./CardWithStats/CardWithStats";
import { FeaturesCard as FeaturesCardPattern } from "./FeaturesCard/FeaturesCard";
import { StatsRingCard as StatsRingCardPattern } from "./StatsRingCard/StatsRingCard";
import { SwitchesCard as SwitchesCardPattern } from "./SwitchesCard/SwitchesCard";
import { TaskCard as TaskCardPattern } from "./TaskCard/TaskCard";

const meta: Meta = { title: "Lookbook/App Cards", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const ActionsGrid: Story = { name: "Card with actions grid", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><ActionsGridPattern /></div> };
export const BadgeCard: Story = { name: "Card with badges", render: () => <div style={{"maxWidth": 370, "margin": "0 auto"}}><BadgeCardPattern /></div> };
export const CardWithStats: Story = { name: "Card with stats", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><CardWithStatsPattern /></div> };
export const FeaturesCard: Story = { name: "Card with icon features", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><FeaturesCardPattern /></div> };
export const StatsRingCard: Story = { name: "Stats card with progress", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><StatsRingCardPattern /></div> };
export const SwitchesCard: Story = { name: "Card with switches", render: () => <div style={{"maxWidth": 540, "margin": "0 auto"}}><SwitchesCardPattern /></div> };
export const TaskCard: Story = { name: "Tasks card", render: () => <div style={{"maxWidth": 360, "margin": "0 auto"}}><TaskCardPattern /></div> };
