import { Typography } from "@mantine/core";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import classes from "./Markdown.module.css";

const components: Components = {
  a: ({ node: _node, children, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>,
};

export default function MarkdownView({ children }: { children: string }) {
  return (
    <Typography className={classes.root}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{children}</ReactMarkdown>
    </Typography>
  );
}
