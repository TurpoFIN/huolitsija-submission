import React from "react";
import { Search, X } from "lucide-react";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/** Search box + segmented filter pills (Linear-style). options: [{value, label, count}] */
export function FilterBar({ search, onSearch, placeholder, filters = [], right, className }) {
  const { t } = useT();
  return (
    <div className={cn("mb-3 flex flex-wrap items-center gap-2", className)}>
      {onSearch && (
        <label className="surface-input flex h-8 w-full sm:w-64 items-center gap-2 rounded-lg px-2.5">
          <Search className="size-4 text-ink-tertiary" aria-hidden />
          <input data-quiet-input value={search || ""} onChange={(e) => onSearch(e.target.value)}
            placeholder={placeholder || t("action.search")} className="min-w-0 flex-1 bg-transparent text-md md:text-sm outline-none placeholder:text-ink-tertiary"
            aria-label={placeholder || t("action.search")} />
          {search && (
            <button type="button" onClick={() => onSearch("")} aria-label={t("action.clear")} className="rounded-full p-0.5 hover:bg-hover">
              <X className="size-3.5" />
            </button>
          )}
        </label>
      )}
      {filters.map((f) => (
        <Segmented key={f.key} value={f.value} onChange={f.onChange} options={f.options} label={f.label} />
      ))}
      {right && <div className="ml-auto flex items-center gap-2">{right}</div>}
    </div>
  );
}

export function Segmented({ value, onChange, options, label, className }) {
  return (
    <div role="radiogroup" aria-label={label} className={cn("inline-flex h-8 items-center rounded-full bg-secondary p-[3px] overflow-x-auto no-scrollbar max-w-full", className)}>
      {options.map((o) => (
        <button key={o.value ?? "all"} type="button" role="radio" aria-checked={value === o.value} onClick={() => onChange(o.value)}
          className={cn("inline-flex h-[26px] items-center gap-1.5 whitespace-nowrap rounded-full px-3 text-sm font-medium text-ink-secondary hover:text-foreground",
            value === o.value && "bg-surface text-foreground shadow-[inset_0_0_0_0.5px_var(--border-default)]")}>
          {o.label}
          {o.count !== undefined && o.count !== null && <span className="tabular text-xs text-ink-tertiary">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}
