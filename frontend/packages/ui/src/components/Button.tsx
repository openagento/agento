import { Button as MantineButton, type ButtonProps } from "@mantine/core";
import type { ButtonHTMLAttributes } from "react";

export type ButtonVariant = "default" | "primary" | "subtle" | "danger";

// `.ag-button` and its modifiers in the kit copy these four Mantine variants.
const MANTINE: Record<ButtonVariant, ButtonProps> = {
  default: { variant: "default" },
  primary: { variant: "filled" },
  subtle: { variant: "subtle" },
  danger: { variant: "light", color: "red" },
};

export function Button({ variant = "default", type = "button", ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return <MantineButton type={type} {...MANTINE[variant]} {...rest} />;
}
