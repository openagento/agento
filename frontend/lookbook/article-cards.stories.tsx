// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { ArticleCard as ArticleCardPattern } from "./ArticleCard/ArticleCard";
import { ArticleCardFooter as ArticleCardFooterPattern } from "./ArticleCardFooter/ArticleCardFooter";
import { ArticleCardImage as ArticleCardImagePattern } from "./ArticleCardImage/ArticleCardImage";
import { ArticleCardVertical as ArticleCardVerticalPattern } from "./ArticleCardVertical/ArticleCardVertical";
import { ArticlesCardsGrid as ArticlesCardsGridPattern } from "./ArticlesCardsGrid/ArticlesCardsGrid";
import { CardGradient as CardGradientPattern } from "./CardGradient/CardGradient";
import { ImageCard as ImageCardPattern } from "./ImageCard/ImageCard";

const meta: Meta = { title: "Lookbook/Article Cards", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const ArticleCard: Story = { name: "Article card with image", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><ArticleCardPattern /></div> };
export const ArticleCardFooter: Story = { name: "Article card with footer", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><ArticleCardFooterPattern /></div> };
export const ArticleCardImage: Story = { name: "Card with background image", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><ArticleCardImagePattern /></div> };
export const ArticleCardVertical: Story = { name: "Vertical article card", render: () => <div style={{"maxWidth": 520, "margin": "0 auto"}}><ArticleCardVerticalPattern /></div> };
export const ArticlesCardsGrid: Story = { name: "Articles cards grid", render: () => <ArticlesCardsGridPattern /> };
export const CardGradient: Story = { name: "Card with gradient border", render: () => <div style={{"maxWidth": 360, "margin": "0 auto"}}><CardGradientPattern /></div> };
export const ImageCard: Story = { name: "Card with image as background", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><ImageCardPattern /></div> };
