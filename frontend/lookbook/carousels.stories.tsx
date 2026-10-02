// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { CardsCarousel as CardsCarouselPattern } from "./CardsCarousel/CardsCarousel";
import { CarouselCard as CarouselCardPattern } from "./CarouselCard/CarouselCard";

const meta: Meta = { title: "Lookbook/Carousels", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const CardsCarousel: Story = { name: "Carousel with cards", render: () => <div style={{"maxWidth": 820, "margin": "0 auto"}}><CardsCarouselPattern /></div> };
export const CarouselCard: Story = { name: "Card with carousel", render: () => <div style={{"maxWidth": 350, "margin": "0 auto"}}><CarouselCardPattern /></div> };
