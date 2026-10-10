import "@mantine/code-highlight/styles.css";
import { CodeHighlight, CodeHighlightAdapterProvider, createHighlightJsAdapter } from "@mantine/code-highlight";
import { Text } from "@mantine/core";
import hljs from "highlight.js/lib/common";
import classes from "./HighlightedCode.module.css";

const adapter = createHighlightJsAdapter(hljs);

/** One fenced code block with syntax colors, its language and a copy button. A lazy chunk of
 *  its own (highlight.js is large): only `<Markdown highlight>` loads it. */
export default function HighlightedCode({ code, language }: { code: string; language?: string }) {
  return (
    <CodeHighlightAdapterProvider adapter={adapter}>
      <div className={classes.root}>
        {language && <Text size="xs" c="dimmed" className={classes.language}>{language}</Text>}
        <CodeHighlight code={code} language={language ?? "plaintext"} copyLabel="Copy" copiedLabel="Copied"
          withBorder radius="sm" />
      </div>
    </CodeHighlightAdapterProvider>
  );
}
