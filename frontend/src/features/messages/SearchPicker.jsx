import React, { useEffect, useId, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import { apiGet } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/**
 * Small inline search picker (no popover, works inside dialogs and on 390 px).
 * pathFor(q) -> API path returning {items}; renderItem(item) -> row content; value = selected item or null.
 */
export default function SearchPicker({ label, placeholder, value, onChange, pathFor, renderItem, renderValue, disabled, hint }) {
  const { t } = useT();
  const id = useId();
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [state, setState] = useState({ items: [], loading: false, error: null });
  const [active, setActive] = useState(0);
  const boxRef = useRef(null);

  useEffect(() => {
    if (!open || value) return undefined;
    const ctrl = new AbortController();
    const timer = setTimeout(async () => {
      setState((s) => ({ ...s, loading: true, error: null }));
      try {
        const r = await apiGet(pathFor(q.trim()), { signal: ctrl.signal });
        setState({ items: (r?.items || []).slice(0, 8), loading: false, error: null });
        setActive(0);
      } catch (e) {
        if (e.name !== "AbortError") setState({ items: [], loading: false, error: e });
      }
    }, 220);
    return () => { clearTimeout(timer); ctrl.abort(); };
  }, [q, open, value, pathFor]);

  useEffect(() => {
    if (!open) return undefined;
    const onDoc = (e) => { if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const pick = (item) => { onChange(item); setOpen(false); setQ(""); };

  if (value) {
    return (
      <div className="flex flex-col gap-1">
        <span className="text-xs text-ink-secondary">{label}</span>
        <div className="flex min-h-8 items-center gap-2 rounded-lg bg-[var(--bg-subtle)] px-2.5 py-1 text-sm">
          <span className="min-w-0 flex-1 truncate">{renderValue ? renderValue(value) : renderItem(value)}</span>
          {!disabled && (
            <button type="button" onClick={() => onChange(null)} aria-label={t("messages.new.clear")} className="rounded-full p-0.5 hover:bg-hover">
              <X className="size-3.5" />
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="relative flex flex-col gap-1" ref={boxRef}>
      <label htmlFor={id} className="text-xs text-ink-secondary">{label}</label>
      <div className="surface-input flex h-8 items-center gap-2 rounded-lg px-2.5">
        <Search className="size-4 text-ink-tertiary" aria-hidden />
        <input id={id} value={q} disabled={disabled} placeholder={placeholder} autoComplete="off"
          role="combobox" aria-expanded={open} aria-controls={`${id}-list`}
          onFocus={() => setOpen(true)} onChange={(e) => { setQ(e.target.value); setOpen(true); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, state.items.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
            else if (e.key === "Enter" && open && state.items[active]) { e.preventDefault(); pick(state.items[active]); }
            else if (e.key === "Escape" && open) { e.stopPropagation(); setOpen(false); }
          }}
          className="min-w-0 flex-1 bg-transparent text-md outline-none placeholder:text-ink-tertiary md:text-sm" />
      </div>
      {hint && <span className="text-xs text-ink-tertiary">{hint}</span>}
      {open && (
        <ul id={`${id}-list`} role="listbox"
          className="surface-overlay absolute left-0 right-0 top-[54px] z-10 max-h-56 overflow-y-auto rounded-lg p-1">
          {state.loading && state.items.length === 0 && <li className="px-2 py-1.5 text-sm text-ink-tertiary">{t("messages.new.searching")}</li>}
          {state.error && <li className="px-2 py-1.5 text-sm text-danger-fg">{state.error.message}</li>}
          {!state.loading && !state.error && state.items.length === 0 && <li className="px-2 py-1.5 text-sm text-ink-tertiary">{t("messages.new.no_results")}</li>}
          {state.items.map((it, i) => (
            <li key={it.id} role="option" aria-selected={i === active}>
              <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => pick(it)} onMouseEnter={() => setActive(i)}
                className={cn("flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm", i === active && "bg-hover")}>
                {renderItem(it)}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
