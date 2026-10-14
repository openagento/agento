import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, within } from "storybook/test";
import { Markdown } from "./Markdown";

const meta: Meta = { title: "Components/Markdown" };
export default meta;

const SAMPLE = `## Summary

The import **finished**. See [the log](https://example.com/log) and run \`bin/agento replay 42\`.

- 120 rows read
- 3 rows skipped

\`\`\`json
{"ok": true}
\`\`\`

| Step | State |
|---|---|
| Read | done |
| Write | done |

> Next run at 02:00.

<script>alert(1)</script>`;

export const Prose: StoryObj = {
  render: () => <Markdown>{SAMPLE}</Markdown>,
  play: async ({ canvasElement }) => {
    await within(canvasElement).findByRole("table", {}, { timeout: 10_000 });
    await expect(canvasElement.querySelector("script")).toBeNull();
  },
};
