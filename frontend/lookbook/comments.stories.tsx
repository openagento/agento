// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { CommentHtml as CommentHtmlPattern } from "./CommentHtml/CommentHtml";
import { CommentSimple as CommentSimplePattern } from "./CommentSimple/CommentSimple";

const meta: Meta = { title: "Lookbook/Comments", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const CommentHtml: Story = { name: "Comment with html content", render: () => <div style={{"maxWidth": 380, "margin": "0 auto"}}><CommentHtmlPattern /></div> };
export const CommentSimple: Story = { name: "Comment", render: () => <div style={{"maxWidth": 380, "margin": "0 auto"}}><CommentSimplePattern /></div> };
