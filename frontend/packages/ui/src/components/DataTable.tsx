// A server-paged table: it sorts the rows it has and never pages on the client (the
// API bounds the page). Rows take focus with the arrow keys; Enter calls `onRowActivate`.
import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Table, UnstyledButton } from "@mantine/core";
import {
  createColumnHelper, createSortedRowModel, rowSortingFeature, tableFeatures, useTable, type RowData,
} from "@tanstack/react-table";
import { EmptyState, ErrorState, LoadingState } from "./States";
import css from "./DataTable.module.css";

export interface Column<T extends RowData> {
  id: string;
  header: string;
  cell: (row: T) => ReactNode;
  /** Present → the column sorts by this value. */
  sortValue?: (row: T) => string | number;
  /** Keep the cell on one line, whole: a badge, a time. Auto layout would cut a badge to "S…". */
  nowrap?: boolean;
  /** Long text that wraps: it keeps a readable width, and a narrow screen scrolls the table
   *  instead of squeezing it to one letter beside the `nowrap` columns. */
  prose?: boolean;
}

const features = tableFeatures({ rowSortingFeature, sortedRowModel: createSortedRowModel() });

export function DataTable<T extends RowData>({ columns, rows, rowKey, loading, error, onRetry, emptyTitle = "Nothing here yet",
  emptyText, caption, onRowActivate }: {
  columns: Column<T>[]; rows: T[] | undefined; rowKey: (row: T) => string;
  loading?: boolean; error?: string | null; onRetry?: () => void;
  emptyTitle?: string; emptyText?: ReactNode; caption: string; onRowActivate?: (row: T) => void;
}) {
  const data = rows ?? (EMPTY as T[]);
  const [active, setActive] = useState(0);
  const body = useRef<HTMLTableSectionElement>(null);
  const helper = createColumnHelper<typeof features, T>();
  const defs = helper.columns(columns.map((c) => helper.accessor((r: T) => (c.sortValue ? c.sortValue(r) : ""), {
    id: c.id,
    header: c.header,
    enableSorting: Boolean(c.sortValue),
  })));
  // Called, not rendered through FlexRender: a cell function made here is a new component type on
  // every render, so React would remount every cell (an open select closes when a row takes focus).
  const cellOf = new Map(columns.map((c) => [c.id, c.cell]));
  const wrapOf = new Map(columns.map((c) => [c.id, c.nowrap ? css.nowrap : c.prose ? css.prose : undefined]));
  const table = useTable({ features, columns: defs, data, getRowId: (r: T) => rowKey(r) });

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={onRetry} />;
  if (!data.length) return <EmptyState title={emptyTitle}>{emptyText}</EmptyState>;

  const tableRows = table.getRowModel().rows;
  const focusRow = (i: number) => {
    const next = Math.max(0, Math.min(tableRows.length - 1, i));
    setActive(next);
    (body.current?.rows[next] as HTMLElement | undefined)?.focus();
  };
  const onKey = (e: KeyboardEvent<HTMLTableRowElement>, i: number, row: T) => {
    if (e.target !== e.currentTarget) return; // a control in a cell (select, button) owns its keys
    if (e.key === "ArrowDown") { e.preventDefault(); focusRow(i + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); focusRow(i - 1); }
    else if (e.key === "Home") { e.preventDefault(); focusRow(0); }
    else if (e.key === "End") { e.preventDefault(); focusRow(tableRows.length - 1); }
    else if (e.key === "Enter" && onRowActivate) onRowActivate(row);
  };

  return (
    <Table.ScrollContainer minWidth={0} type="native">
      <Table>
        <caption className="ag-visually-hidden">{caption}</caption>
        <Table.Thead>
          {table.getHeaderGroups().map((g) => (
            <Table.Tr key={g.id}>
              {g.headers.map((h) => {
                const sorted = h.column.getIsSorted();
                return (
                  <Table.Th key={h.id} scope="col"
                    aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}>
                    {h.column.getCanSort()
                      ? <UnstyledButton fz="inherit" fw="inherit" onClick={h.column.getToggleSortingHandler()}><table.FlexRender header={h} /></UnstyledButton>
                      : <table.FlexRender header={h} />}
                  </Table.Th>
                );
              })}
            </Table.Tr>
          ))}
        </Table.Thead>
        <Table.Tbody ref={body}>
          {tableRows.map((r, i) => (
            <Table.Tr key={r.id} tabIndex={i === Math.min(active, tableRows.length - 1) ? 0 : -1}
              onKeyDown={(e) => onKey(e, i, r.original)} onFocus={() => setActive(i)}>
              {r.getAllCells().map((c) => {
                const content = cellOf.get(c.column.id)?.(r.original);
                const wrap = wrapOf.get(c.column.id);
                return <Table.Td key={c.id}>{wrap ? <div className={wrap}>{content}</div> : content}</Table.Td>;
              })}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

const EMPTY: never[] = [];
