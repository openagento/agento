import type { ButtonHTMLAttributes } from "react";

export type ButtonVariant = "default" | "primary" | "subtle" | "danger";

export function Button({ variant = "default", className, type = "button", ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  const cls = ["ag-button", variant !== "default" && `ag-button--${variant}`, className].filter(Boolean).join(" ");
  return <button type={type} className={cls} {...rest} />;
}
