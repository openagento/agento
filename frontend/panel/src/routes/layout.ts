// Layout values the panel routes share, each derived from the theme (UI-1).
import classes from "./layout.module.css";

/** Half the theme's xs spacing: between a name and the lines under it, and between label pills. */
export const TIGHT = "calc(var(--mantine-spacing-xs) / 2)";

/** Admin data (a label name or key, up to 100 characters) breaks anywhere instead of overflowing. */
export const wrapAnywhere = classes.wrap;

/** For a TagsInput or MultiSelect `classNames.pill`: a long selected name wraps instead of being cut. */
export const wrapPill = classes.pill;
