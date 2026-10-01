export type BadgeTone =
  | "pending" | "published" | "running" | "succeeded" | "failed" | "blocked" | "neutral" | "info";

/** The text names the state; the color is a second signal, never the only one. */
export function StatusBadge({ tone, children }: { tone: BadgeTone; children: React.ReactNode }) {
  return <span className={`ag-badge ag-badge--${tone}`}>{children}</span>;
}
