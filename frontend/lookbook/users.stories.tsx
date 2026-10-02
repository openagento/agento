// Lookbook: patterns copied 1:1 from ui.mantine.dev (MIT, see LICENCE in this directory).
// A reference catalogue, not tests: tagged "!test" (third-party code, remote images).
import type { Meta, StoryObj } from "@storybook/react-vite";
import { UserButton as UserButtonPattern } from "./UserButton/UserButton";
import { UserCardImage as UserCardImagePattern } from "./UserCardImage/UserCardImage";
import { UserInfoAction as UserInfoActionPattern } from "./UserInfoAction/UserInfoAction";
import { UserInfoIcons as UserInfoIconsPattern } from "./UserInfoIcons/UserInfoIcons";
import { UserMenu as UserMenuPattern } from "./UserMenu/UserMenu";
import { UsersRolesTable as UsersRolesTablePattern } from "./UsersRolesTable/UsersRolesTable";
import { UsersStack as UsersStackPattern } from "./UsersStack/UsersStack";
import { UsersTable as UsersTablePattern } from "./UsersTable/UsersTable";

const meta: Meta = { title: "Lookbook/Users", tags: ["!test"] };
export default meta;
type Story = StoryObj;

export const UserButton: Story = { name: "User button", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><UserButtonPattern /></div> };
export const UserCardImage: Story = { name: "User card with image", render: () => <div style={{"maxWidth": 300, "margin": "0 auto"}}><UserCardImagePattern /></div> };
export const UserInfoAction: Story = { name: "User card with action", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><UserInfoActionPattern /></div> };
export const UserInfoIcons: Story = { name: "User info with icons", render: () => <div style={{"maxWidth": 320, "margin": "0 auto"}}><UserInfoIconsPattern /></div> };
export const UserMenu: Story = { name: "User menu", render: () => <UserMenuPattern /> };
export const UsersRolesTable: Story = { name: "Table with roles select", render: () => <div style={{"maxWidth": 800, "margin": "0 auto"}}><UsersRolesTablePattern /></div> };
export const UsersStack: Story = { name: "Users stack", render: () => <div style={{"maxWidth": 800, "margin": "0 auto"}}><UsersStackPattern /></div> };
export const UsersTable: Story = { name: "Table with users", render: () => <div style={{"maxWidth": 800, "margin": "0 auto"}}><UsersTablePattern /></div> };
