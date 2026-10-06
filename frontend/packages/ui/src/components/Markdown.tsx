import { lazy, memo, Suspense } from "react";

// react-markdown loads on first use, so the panel's entry chunk stays inside its budget.
const MarkdownView = lazy(() => import("./MarkdownView"));

/** Markdown (GitHub flavour) as prose. Raw HTML is shown as text, never rendered. Until the
 *  renderer loads, the plain text shows. `.ag-prose` is the miniapp twin. Memoized: the parser
 *  runs on each render, and a stream frame re-renders the whole timeline. */
export const Markdown = memo(function Markdown({ children }: { children: string }) {
  return (
    <Suspense fallback={<div className="ag-card__body--pre">{children}</div>}>
      <MarkdownView>{children}</MarkdownView>
    </Suspense>
  );
});
