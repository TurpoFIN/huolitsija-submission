// Thin wrapper over the kit DataTable for finance: horizontal scroll inside its own container, and an optional
// totals row whose cells follow the same columns (alignment, responsive hiding).
import React from "react";
import { DataTable } from "@/components/app/DataTable";
import { TableCell, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

const hide = (c) => (c.hideBelow === "md" ? "hidden md:table-cell" : c.hideBelow === "lg" ? "hidden lg:table-cell" : "");

export function FinTable({ columns, rows, totals, totalsLabel, className, ...rest }) {
  const footer = totals ? (
    <TableRow className="border-t-2 border-[color:var(--border-default)] font-medium hover:bg-transparent">
      {columns.map((c, i) => (
        <TableCell key={c.key} className={cn("py-1.5", c.align === "right" && "text-right tabular", hide(c), c.className)}>
          {c.total ? c.total(totals) : i === 0 ? totalsLabel : null}
        </TableCell>
      ))}
    </TableRow>
  ) : null;
  return (
    <div className={cn("min-w-0 overflow-x-auto", className)}>
      <DataTable columns={columns} rows={rows} dense footer={footer} {...rest} />
    </div>
  );
}
