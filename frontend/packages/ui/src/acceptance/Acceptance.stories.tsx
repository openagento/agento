// PRD E8 §10.3: one <JobStatusCard>, one implementation, rendered in the panel and as the
// example miniapp's markup over the same agento-ui.css. Equal computed styles prove that
// one stylesheet serves both runtimes.
import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect } from "storybook/test";
import { JobStatusCard } from "../components/JobStatusCard";
import { EXAMPLE_PROPS, exampleCardHtml, styleDiff } from "./exampleCard";

function SideBySide() {
  return (
    <div className="ag-row" style={{ alignItems: "stretch" }}>
      <section aria-label="Panel component" data-side="panel" style={{ width: 320 }}>
        <JobStatusCard {...EXAMPLE_PROPS} />
      </section>
      <section aria-label="Miniapp markup" data-side="miniapp" style={{ width: 320 }}
        dangerouslySetInnerHTML={{ __html: exampleCardHtml() }} />
    </div>
  );
}

const meta: Meta = { title: "Acceptance/JobStatusCard", render: () => <SideBySide /> };
export default meta;

const play: StoryObj["play"] = async ({ canvasElement }) => {
  const panel = canvasElement.querySelector('[data-side="panel"] > article');
  const miniapp = canvasElement.querySelector('[data-side="miniapp"] > article');
  await expect(panel).not.toBeNull();
  await expect(miniapp).not.toBeNull();
  await expect(styleDiff(panel!, miniapp!)).toEqual([]);
};

export const Light: StoryObj = { globals: { theme: "light" }, play };
export const Dark: StoryObj = { globals: { theme: "dark" }, play };
