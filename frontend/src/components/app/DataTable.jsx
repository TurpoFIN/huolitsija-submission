import React, { useMemo, useState, useEffect } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { LoadingRows } from "@/components/app/states";
import { Button } from "@/components/ui/button";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/**
 * columns: [{ key, header, cell?: (row) => node, sort?: (row) => value, align?: "right", className?, hideBelow?: "md"|"lg" }]
 * Numbers/currency: align "right" + tabular. Wide tables scroll inside the container.
 */
export function DataTable({ columns, rows, rowKey = "id", onRowClick, loading, empty, defaultSort, dense, className, rowClassName, footer, pageSize, pageLabel }) {
  const { t } = useT();
  const [page, setPage] = useState(0);
  const [sort, setSort] = useState(defaultSort || null); // { key, dir }
  const sorted = useMemo(() => {
    if (!rows || !sort) return rows || [];
    const col = columns.find((c) => c.key === sort.key);
    if (!col) return rows;
    const get = col.sort || ((r) => r[col.key]);
    return [...rows].sort((a, b) => {
      if (col.compare) return col.compare(a,b) * (sort.dir === "asc" ? 1 : -1);
      const va = get(a);
      const vb = get(b);
      if (va === vb) return 0;
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      return (va > vb ? 1 : -1) * (sort.dir === "asc" ? 1 : -1);
    });
  }, [rows, sort, columns]);

  useEffect(() => setPage(0), [sort]);
  const pages = pageSize ? Math.max(1,Math.ceil(sorted.length/pageSize)) : 1;
  const current = Math.min(page,pages-1);
  const visibleRows = pageSize ? sorted.slice(current*pageSize,(current+1)*pageSize) : sorted;

  if (loading && !rows) return <LoadingRows />;
  if (rows && rows.length === 0 && empty) return empty;
  const hide = (c) => (c.hideBelow === "md" ? "hidden md:table-cell" : c.hideBelow === "lg" ? "hidden lg:table-cell" : "");

  return (
    <div>
    <Table className={className}>
      <TableHeader>
        <TableRow>
          {columns.map((c) => {
            const active = sort?.key === c.key;
            return (
              <TableHead key={c.key} className={cn(c.align === "right" && "text-right", hide(c), c.headClassName)}
                title={c.headTitle} aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}>
                {c.sortable === false || !c.header ? (
                  c.header
                ) : (
                  <button type="button" className={cn("inline-flex items-center gap-1 hover:text-foreground", active && "text-foreground")}
                    onClick={() => setSort((s) => (s?.key === c.key ? { key: c.key, dir: s.dir === "asc" ? "desc" : "asc" } : { key: c.key, dir: "asc" }))}>
                    {c.header}
                    {active && (sort.dir === "asc" ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                  </button>
                )}
              </TableHead>
            );
          })}
        </TableRow>
      </TableHeader>
      <TableBody>
        {visibleRows.map((r) => (
          <TableRow
            key={typeof rowKey === "function" ? rowKey(r) : r[rowKey]}
            onClick={onRowClick ? () => onRowClick(r) : undefined}
            onKeyDown={onRowClick ? (e) => { if (e.key === "Enter") onRowClick(r); } : undefined}
            tabIndex={onRowClick ? 0 : undefined}
            className={cn(onRowClick && "cursor-pointer", rowClassName?.(r))}
          >
            {columns.map((c) => (
              <TableCell key={c.key} className={cn(dense && "py-1.5", c.align === "right" && "text-right tabular", hide(c), c.className)}>
                {c.cell ? c.cell(r) : r[c.key] ?? <span className="text-ink-tertiary">—</span>}
              </TableCell>
            ))}
          </TableRow>
        ))}
        {footer}
      </TableBody>
    </Table>
    {pageSize && pages > 1 && <nav aria-label={t(pageLabel || "review.rates.page",{page:current+1,pages,n:sorted.length})} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs"><span>{t(pageLabel || "review.rates.page",{page:current+1,pages,n:sorted.length})}</span><span className="flex gap-2"><Button variant="outline" size="xs" disabled={!current} onClick={()=>setPage(current-1)}>{t("review.previous")}</Button><Button variant="outline" size="xs" disabled={current>=pages-1} onClick={()=>setPage(current+1)}>{t("review.next")}</Button></span></nav>}
    </div>
  );
}
