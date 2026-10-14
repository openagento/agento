import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";
import { StatusBadge } from "./StatusBadge";
import { DeleteAction, SettingsAction } from "./IconAction";
import { AppTile, TileGrid, ViewToggle, type TileMode } from "./Tiles";

const meta: Meta = { title: "Components/Tiles" };
export default meta;
type Story = StoryObj;

function Toggle() {
  const [mode, setMode] = useState<TileMode>("tiles");
  return <ViewToggle value={mode} onChange={setMode} />;
}

export const Switch: Story = { render: () => <Toggle /> };

export const Apps: Story = {
  render: () => (
    <TileGrid label="Apps">
      <AppTile icon="mail" title="Jira approver" onOpen={() => {}}
        badge={<StatusBadge tone="info">New version waiting</StatusBadge>} actions={<><SettingsAction /><DeleteAction /></>} />
      <AppTile icon="table" title="Two views" onOpen={() => {}} choices={[{ value: "3", label: "Support" }, { value: "4", label: "Ops" }]} />
      <AppTile icon={null} title="View only" disabled onOpen={() => {}} />
    </TileGrid>
  ),
};
