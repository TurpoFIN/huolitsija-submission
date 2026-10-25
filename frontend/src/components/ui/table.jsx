import * as React from "react";
import { cn } from "@/lib/utils";

// Horizontal overflow belongs to the table container, never the page. 32px header, compact rows.
const Table = React.forwardRef(({ className, containerClassName, ...props }, ref) => (
  <div className={cn("relative w-full overflow-x-auto", containerClassName)}>
    <table ref={ref} className={cn("w-full caption-bottom text-sm border-collapse", className)} {...props} />
  </div>
));
Table.displayName = "Table";
const TableHeader = React.forwardRef(({ className, ...props }, ref) => (
  <thead ref={ref} className={cn("[&_tr]:border-b [&_tr]:hover:bg-transparent", className)} {...props} />
));
TableHeader.displayName = "TableHeader";
const TableBody = React.forwardRef(({ className, ...props }, ref) => (
  <tbody ref={ref} className={cn("[&_tr:last-child]:border-0", className)} {...props} />
));
TableBody.displayName = "TableBody";
const TableFooter = React.forwardRef(({ className, ...props }, ref) => (
  <tfoot ref={ref} className={cn("border-t font-medium", className)} {...props} />
));
TableFooter.displayName = "TableFooter";
const TableRow = React.forwardRef(({ className, ...props }, ref) => (
  <tr ref={ref} className={cn("border-b border-[color:var(--border-subtle)] transition-colors hover:bg-hover data-[state=selected]:bg-hover", className)} {...props} />
));
TableRow.displayName = "TableRow";
const TableHead = React.forwardRef(({ className, ...props }, ref) => (
  <th ref={ref} className={cn("h-8 px-3 text-left align-middle text-xs font-medium text-ink-tertiary whitespace-nowrap", className)} {...props} />
));
TableHead.displayName = "TableHead";
const TableCell = React.forwardRef(({ className, ...props }, ref) => (
  <td ref={ref} className={cn("px-3 py-2 align-middle", className)} {...props} />
));
TableCell.displayName = "TableCell";
const TableCaption = React.forwardRef(({ className, ...props }, ref) => (
  <caption ref={ref} className={cn("mt-3 text-sm text-ink-tertiary", className)} {...props} />
));
TableCaption.displayName = "TableCaption";

export { Table, TableHeader, TableBody, TableFooter, TableHead, TableRow, TableCell, TableCaption };
