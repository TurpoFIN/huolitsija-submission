/**
 * <RouteText origin destination codes? short? className /> — "Shanghai CNSHA → Lahti FILHI" with LOCODEs in mono.
 *   codes (default true) shows the UN/LOCODE after each name when known. Missing places render "—".
 *   short  use location.city when present ("Tallinn" instead of "Tallinn Old City Harbour") — for lists and summaries.
 */
import React from "react";
import { cn } from "@/lib/utils";
import { locCode, locName } from "./quoteFormat";

function Place({ loc, codes, short }) {
  const code = locCode(loc);
  const name = locName(loc);
  return (
    <span className="inline-flex min-w-0 items-baseline gap-1">
      <span className="truncate" title={short && loc?.city && loc.city !== name ? name : undefined}>{short ? loc?.city || name : name}</span>
      {codes && code && <span className="font-mono text-[11px] text-ink-tertiary">{code}</span>}
    </span>
  );
}

export default function RouteText({ origin, destination, codes = true, short = false, className }) {
  return (
    <span className={cn("inline-flex min-w-0 max-w-full items-baseline gap-1.5", className)}>
      <Place loc={origin} codes={codes} short={short} />
      <span className="text-ink-tertiary">→</span>
      <Place loc={destination} codes={codes} short={short} />
    </span>
  );
}
