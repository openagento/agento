import { lazy, memo, Suspense } from "react";

// react-markdown loads on first use, so the panel's entry chunk stays inside its budget.
const MarkdownView = lazy(() => import("./MarkdownView"));

/** Markdown (GitHub flavour) as prose. Raw HTML is shown as text, never rendered. Until the
 *  renderer loads, the plain text shows. `.ag-prose` is the miniapp twin. Memoized: the parser
 *  runs on each render, and a stream frame re-renders the whole timeline.
 *
 *  `highlight` (opt-in, the chat uses it) renders fenced code with syntax colors, its language and
 *  a copy button, from another lazy chunk. The kit has no twin for that, so the default stays the
 *  plain code block the parity story compares with `.ag-prose`. */
export const Markdown = memo(function Markdown({ children, highlight = false }: { children: string; highlight?: boolean }) {
  return (
    <Suspense fallback={<div className="ag-card__body--pre">{children}</div>}>
      <MarkdownView highlight={highlight}>{children}</MarkdownView>
    </Suspense>
  );
});
