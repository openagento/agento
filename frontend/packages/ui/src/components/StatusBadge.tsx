import { Badge, type MantineColor } from "@mantine/core";
import type { ReactNode } from "react";

export type BadgeTone =
  | "pending" | "published" | "running" | "succeeded" | "failed" | "blocked" | "neutral" | "info";

// `.ag-badge--<tone>` in the kit uses the same Mantine color.
const COLOR: Record<BadgeTone, MantineColor> = {
  pending: "gray", published: "gray", neutral: "gray", running: "blue", info: "blue",
  succeeded: "green", failed: "red", blocked: "yellow",
};

/** The text names the state; the color is a second signal, never the only one. */
export function StatusBadge({ tone, children }: { tone: BadgeTone; children: ReactNode }) {
  return <Badge variant="light" color={COLOR[tone]}>{children}</Badge>;
}
