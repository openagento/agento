// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { AuthenticationForm as AuthenticationFormPattern } from "./AuthenticationForm/AuthenticationForm";
import { AuthenticationImage as AuthenticationImagePattern } from "./AuthenticationImage/AuthenticationImage";
import { AuthenticationTitle as AuthenticationTitlePattern } from "./AuthenticationTitle/AuthenticationTitle";
import { ForgotPassword as ForgotPasswordPattern } from "./ForgotPassword/ForgotPassword";

const meta: Meta = { title: "Lookbook/Authentication", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const AuthenticationForm: Story = { name: "Authentication form", render: () => <div style={{"maxWidth": 420, "margin": "0 auto"}}><AuthenticationFormPattern /></div> };
export const AuthenticationImage: Story = { name: "Authentication page with image", render: () => <AuthenticationImagePattern /> };
export const AuthenticationTitle: Story = { name: "Authentication form with title", render: () => <AuthenticationTitlePattern /> };
export const ForgotPassword: Story = { name: "Forgot password", render: () => <ForgotPasswordPattern /> };
