import {
  Braces, FileArchive, FileChartColumn, FileCode, FileImage, FileSpreadsheet, FileTerminal,
  FileText, FileType, FileVolume, Globe, Paperclip, Presentation, Database, Video,
} from "lucide-react";
import type { ReactNode } from "react";

/** One icon per family of file, keyed by extension. A reader scanning a list of chips sees the
 *  KIND before the name, which is what tells a screenshot from a spreadsheet at a glance. The
 *  map is the whole mechanism: an unknown extension falls back to the paperclip, so a new kind
 *  costs one line here and nothing anywhere else. */
const FAMILY: Record<string, (p: { size: number }) => ReactNode> = {};
const add = (icon: (p: { size: number }) => ReactNode, exts: string) => {
  for (const e of exts.split(" ")) FAMILY[e] = icon;
};

add((p) => <FileImage {...p} />, "png jpg jpeg gif webp avif bmp tiff tif heic heif ico svg psd ai xcf");
add((p) => <Video {...p} />, "mp4 mov avi mkv webm m4v mpg mpeg wmv");
add((p) => <FileVolume {...p} />, "mp3 wav flac aac ogg m4a opus aiff wma mid midi");
add((p) => <Globe {...p} />, "html htm xhtml css scss sass less");
add((p) => <FileCode {...p} />, "js jsx ts tsx py rb php java kt swift go rs c h cpp hpp cs sh bash zsh sql lua pl r scala dart vue svelte");
add((p) => <Braces {...p} />, "json yaml yml toml xml ini env properties lock");
add((p) => <FileSpreadsheet {...p} />, "xlsx xls xlsm xlsb ods csv tsv");
add((p) => <FileText {...p} />, "doc docx odt rtf pages");
add((p) => <FileType {...p} />, "pdf epub mobi djvu ttf otf woff woff2");
add((p) => <Presentation {...p} />, "ppt pptx odp key");
add((p) => <FileArchive {...p} />, "zip tar gz tgz bz2 xz 7z rar zst iso dmg");
add((p) => <Database {...p} />, "db sqlite sqlite3 dump parquet");
add((p) => <FileChartColumn {...p} />, "ipynb dta sav rdata");
add((p) => <FileTerminal {...p} />, "log txt md markdown text out err");

/** The icon for a file name. Unknown (or extension-less) → the paperclip every chip had. */
export function fileIcon(name: string, size = 12): ReactNode {
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
  const Icon = FAMILY[ext];
  return Icon ? Icon({ size }) : <Paperclip size={size} />;
}
