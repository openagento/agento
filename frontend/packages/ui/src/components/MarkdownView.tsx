import { Typography } from "@mantine/core";
import type { Element, ElementContent } from "hast";
import { lazy, Suspense, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { useT } from "../i18n";
import classes from "./Markdown.module.css";

const HighlightedCode = lazy(() => import("./HighlightedCode"));

const components: Components = {
  a: ({ node: _node, children, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>,
};

const text = (n: ElementContent): string =>
  n.type === "text" ? n.value : n.type === "element" ? n.children.map(text).join("") : "";

function ScrollTable(props: ComponentPropsWithoutRef<"table">) {
  const t = useT("ui");
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scroll box must take focus to scroll by key
    <div className={classes.tableScroll} role="region" aria-label={t("markdown.table")} tabIndex={0}><table {...props} /></div>
  );
}

/** A fenced block (`pre > code`) goes to `HighlightedCode`; anything else stays a plain `pre`. A
 *  table scrolls in its own box, which the keyboard can reach (axe scrollable-region-focusable). */
const highlighted: Components = {
  ...components,
  table: ({ node: _node, ...props }) => <ScrollTable {...props} />,
  pre: ({ node, children, ...props }) => {
    const code = node?.children[0];
    if (code?.type !== "element" || code.tagName !== "code") return <pre {...props}>{children}</pre>;
    const lang = ((code as Element).properties.className as string[] | undefined)
      ?.find((c) => c.startsWith("language-"))?.slice("language-".length);
    return (
      <Suspense fallback={<pre {...props}>{children}</pre>}>
        <HighlightedCode code={text(code).replace(/\n$/, "")} language={lang} />
      </Suspense>
    );
  },
};

export default function MarkdownView({ children, highlight = false }: { children: string; highlight?: boolean }) {
  return (
    <Typography className={classes.root}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={highlight ? highlighted : components}>{children}</ReactMarkdown>
    </Typography>
  );
}
