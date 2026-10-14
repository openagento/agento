// Every in-panel navigation link. Mantine's `Anchor` takes its color from `--mantine-color-anchor`
// (the theme's link color, the same value the kit gives a miniapp as `--ag-color-link`), which a bare
// router `Link` does not: it would render in the browser default, visited purple and all.
import { Anchor, type AnchorProps } from "@mantine/core";
import { Link, type LinkProps } from "react-router";

export function AppLink(props: LinkProps & AnchorProps) {
  return <Anchor component={Link} {...props} />;
}
