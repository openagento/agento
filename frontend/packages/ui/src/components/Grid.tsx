// The parts of a server grid (docs/development/frontend.md "Server grids"): the URL state the API
// query is built from, and the pager under the table. The server parses the same parameters
// (src/agento/web/grid.py).
import { useEffect, useRef, useState } from "react";
import { Group, Text } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useSearchParams } from "react-router";
import { IconAction } from "./IconAction";
import type { Sort } from "./DataTable";
import { useT } from "../i18n";

/** src/agento/web/grid.py MAX_PAGE: the server refuses a higher page. */
export const MAX_PAGE = 100_000;
/** src/agento/web/grid.py MAX_Q: the server refuses a longer search. */
const MAX_Q = 200;
export const SEARCH_DEBOUNCE_MS = 300;

export const lastPage = (total: number, pageSize: number) =>
  Math.max(1, Math.min(Math.ceil(total / pageSize), MAX_PAGE));

// Python's str.strip() set (str.isspace: 29 code points, all single UTF-16 units). It differs from
// String.trim(): it has U+001C–U+001F and U+0085, and not U+FEFF.
const SPACE = new Set([..."\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2028\u2029\u202f\u205f\u3000"]
  .concat(Array.from({ length: 11 }, (_, i) => String.fromCharCode(0x2000 + i))));

/** `v` without the edge spaces grid.py strips. Each end is scanned inward once: linear in `v`. */
function strip(v: string) {
  let start = 0;
  let end = v.length;
  while (start < end && SPACE.has(v[start])) start++;
  while (end > start && SPACE.has(v[end - 1])) end--;
  return v.slice(start, end);
}

/** The first `max` code points of `v` (grid.py counts code points, not UTF-16 units), and whether
 *  there were more. It reads at most `max + 1` code points of a long paste. */
function head(v: string, max: number) {
  let taken = 0;
  let at = 0;
  for (const c of v) {
    if (taken === max) return { text: v.slice(0, at), more: true };
    taken++;
    at += c.length;
  }
  return { text: v, more: false };
}

const positive = (v: string | null) => (v && /^[0-9]{1,6}$/.test(v) && Number(v) >= 1 ? Number(v) : 1);

/** `page`, `q`, `sort`, `dir` and the page's own `filters` live in the URL, so Back restores the grid.
 *  Any change but a page change goes back to page 1. The search box is instant; the URL (and so the
 *  API query) follows after a pause in typing. A URL value the server would refuse (a sort not in
 *  `sorts`, a filter value not listed, a too-long search) reads as its default: an old or hand-made
 *  link shows the grid, not an error. */
export function useGridParams({ defaultSort, sorts, filters = {} }: {
  defaultSort: Sort; sorts: readonly string[]; filters?: Record<string, readonly string[]>;
}) {
  const t = useT("ui");
  const [params, setParams] = useSearchParams();
  const page = Math.min(positive(params.get("page")), MAX_PAGE);
  // A shared URL with a longer search is cut to what the server accepts; the box refuses to send one.
  const q = head(strip(params.get("q") ?? ""), MAX_Q).text;
  // The sort the server will use, resolved once: an unknown key is the default key, and a missing or
  // unknown `dir` is the default direction (grid.py does the same for `dir` alone).
  const rawSort = params.get("sort");
  const dir = params.get("dir");
  const sort: Sort = {
    id: rawSort !== null && sorts.includes(rawSort) ? rawSort : defaultSort.id,
    desc: dir === "asc" ? false : dir === "desc" ? true : defaultSort.desc,
  };
  const isDefault = sort.id === defaultSort.id && sort.desc === defaultSort.desc;
  const names = Object.keys(filters);

  const update = (change: (n: URLSearchParams) => void, { keepPage = false, replace = false } = {}) =>
    setParams((p) => {
      const n = new URLSearchParams(p);
      change(n);
      if (!keepPage) n.delete("page");
      return n;
    }, { replace });

  const [search, setSearch] = useState(q);
  const [settled] = useDebouncedValue(search, SEARCH_DEBOUNCE_MS);
  const written = useRef(q);
  useEffect(() => {
    if (q !== written.current) { written.current = q; setSearch(q); } // Back, or Clear filters
  }, [q]);
  const typed = head(strip(search), MAX_Q);
  useEffect(() => {
    const next = head(strip(settled), MAX_Q);
    if (next.more || next.text === written.current) return; // a too-long search waits for a fix
    written.current = next.text;
    update((n) => (next.text ? n.set("q", next.text) : n.delete("q")), { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `update` is new each render; only the settled text matters
  }, [settled]);

  const filterValues: Record<string, string | null> = Object.fromEntries(names.map((f) => {
    const v = params.get(f);
    return [f, v !== null && filters[f].includes(v) ? v : null];
  }));
  const query = new URLSearchParams({ page: String(page) });
  if (q) query.set("q", q);
  if (!isDefault) { query.set("sort", sort.id); query.set("dir", sort.desc ? "desc" : "asc"); }
  for (const f of names) if (filterValues[f] !== null) query.set(f, filterValues[f]!);

  return {
    page, q, search, sort, filters: filterValues,
    /** The query string for the API: always has `page`, so the API answers one page. */
    query: query.toString(),
    filtered: Boolean(q) || names.some((f) => filterValues[f] !== null),
    /** The box holds more than the server searches: show it, and nothing is sent until it fits. */
    searchError: typed.more ? t("grid.search_too_long", { max: MAX_Q }) : null,
    setSearch,
    setPage: (p: number, opts?: { replace?: boolean }) =>
      update((n) => (p > 1 ? n.set("page", String(p)) : n.delete("page")), { keepPage: true, ...opts }),
    /** `null` is the default sort. A header that asks for it while the grid already shows it (the
     *  default column sorts the other way first) gets the other direction, so a click always moves. */
    setSort: (asked: Sort | null) => update((n) => {
      const s = asked ?? (isDefault ? { id: defaultSort.id, desc: !defaultSort.desc } : null);
      if (!s || (s.id === defaultSort.id && s.desc === defaultSort.desc)) { n.delete("sort"); n.delete("dir"); }
      else { n.set("sort", s.id); n.set("dir", s.desc ? "desc" : "asc"); }
    }),
    setFilter: (name: string, value: string | null) => update((n) => (value === null ? n.delete(name) : n.set(name, value))),
    clear: () => {
      written.current = "";
      setSearch("");
      update((n) => { n.delete("q"); for (const f of names) n.delete(f); });
    },
  };
}

/** "1–50 of 1,234" and Previous / Next. Nothing when everything fits on one page. Put it right
 *  after its grid: a page change scrolls that element's top back into view. */
export function Pager({ page, pageSize, total, onPage }: {
  page: number; pageSize: number; total: number; onPage: (page: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const t = useT("ui");
  if (total <= pageSize) return null;
  const last = lastPage(total, pageSize);
  // A new page starts at its first row: bring the grid's top back into view, under a fixed header.
  const go = (p: number) => {
    onPage(p);
    const grid = ref.current?.previousElementSibling;
    const below = document.querySelector("header")?.getBoundingClientRect().bottom ?? 0;
    const top = grid?.getBoundingClientRect().top;
    if (top !== undefined && top < below) window.scrollBy({ top: top - below });
  };
  const from = Math.min((page - 1) * pageSize + 1, total);
  const to = Math.min(page * pageSize, total);
  return (
    <Group ref={ref} justify="flex-end" gap="xs" wrap="nowrap">
      <Text size="sm" c="dimmed" aria-live="polite">{t("grid.range", { from, to, total })}</Text>
      <IconAction label={t("grid.previous")} icon={<ChevronLeft size={16} />} disabled={page <= 1} onClick={() => go(page - 1)} />
      <IconAction label={t("grid.next")} icon={<ChevronRight size={16} />} disabled={page >= last} onClick={() => go(page + 1)} />
    </Group>
  );
}
