// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { NotFoundImage as NotFoundImagePattern } from "./NotFoundImage/NotFoundImage";
import { NotFoundTitle as NotFoundTitlePattern } from "./NotFoundTitle/NotFoundTitle";
import { NothingFoundBackground as NothingFoundBackgroundPattern } from "./NothingFoundBackground/NothingFoundBackground";
import { ServerError as ServerErrorPattern } from "./ServerError/ServerError";
import { ServerOverload as ServerOverloadPattern } from "./ServerOverload/ServerOverload";

const meta: Meta = { title: "Lookbook/Error Pages", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const NotFoundImage: Story = { name: "404 page with image", render: () => <NotFoundImagePattern /> };
export const NotFoundTitle: Story = { name: "404 page", render: () => <NotFoundTitlePattern /> };
export const NothingFoundBackground: Story = { name: "404 as background image", render: () => <NothingFoundBackgroundPattern /> };
export const ServerError: Story = { name: "500 page", render: () => <ServerErrorPattern /> };
export const ServerOverload: Story = { name: "503 page", render: () => <ServerOverloadPattern /> };
