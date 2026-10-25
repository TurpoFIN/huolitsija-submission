/**
 * WarningsList — compact list of quote warnings, one line each, level glyph in the status ink.
 *
 * Props (stable):
 *   warnings   Warning[]  {code, level "info"|"warning"|"error", message, params}
 *   className
 *   dense      bool  smaller text (inside option rows / side panels)
 * Duplicates (same code + params) are shown once. Errors first, then warnings, then info. Renders nothing when empty.
 */
import React from "react";
import { CircleAlert, Info, TriangleAlert } from "lucide-react";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { warningText } from "./quoteFormat";

const LEVEL = {
  error: { Icon: CircleAlert, cls: "text-danger-fg", order: 0 },
  warning: { Icon: TriangleAlert, cls: "text-warning-fg", order: 1 },
  info: { Icon: Info, cls: "text-ink-tertiary", order: 2 },
};

export default function WarningsList({ warnings, className, dense }) {
  const { t, locale } = useT();
  if (!Array.isArray(warnings) || warnings.length === 0) return null;
  const seen = new Set();
  const list = warnings
    .filter((w) => {
      const k = `${w.code}|${JSON.stringify(w.params || {})}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    })
    .sort((a, b) => (LEVEL[a.level]?.order ?? 2) - (LEVEL[b.level]?.order ?? 2));
  return (
    <ul className={cn("flex flex-col gap-1", className)}>
      {list.map((w, i) => {
        const L = LEVEL[w.level] || LEVEL.info;
        return (
          <li key={i} className={cn("flex items-start gap-2", dense ? "text-xs" : "text-sm")} data-code={w.code}>
            <L.Icon className={cn("mt-0.5 shrink-0", dense ? "size-3.5" : "size-4", L.cls)} aria-label={t(`quotes.level.${w.level || "info"}`)} />
            <span className="min-w-0 text-ink-secondary">{warningText(t, w, locale)}</span>
          </li>
        );
      })}
    </ul>
  );
}
