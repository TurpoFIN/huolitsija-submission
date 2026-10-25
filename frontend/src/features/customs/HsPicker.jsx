// HS nomenclature typeahead (GET /api/hs/search). Picks a 6-digit HS subheading; the broker then sets and confirms the CN code.
import React, { useEffect, useRef, useState } from "react";
import { Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { apiGet } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { fmtCode, useDebounced } from "./common";

export default function HsPicker({ onPick, disabled, initial = "", label }) {
  const { t } = useT();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState(initial);
  const dq = useDebounced(q, 220);
  const [state, setState] = useState({ items: [], loading: false, error: null });
  const [active, setActive] = useState(0);
  const inputRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const query = dq.trim();
    if (query.length < 2) {
      setState({ items: [], loading: false, error: null });
      return undefined;
    }
    const c = new AbortController();
    setState((s) => ({ ...s, loading: true, error: null }));
    apiGet(`/hs/search?q=${encodeURIComponent(query)}&limit=12`, { signal: c.signal })
      .then((r) => { setState({ items: r.items || [], loading: false, error: null }); setActive(0); })
      .catch((e) => { if (e.name !== "AbortError") setState({ items: [], loading: false, error: e }); });
    return () => c.abort();
  }, [dq, open]);

  const pick = (it) => {
    setOpen(false);
    onPick(it);
  };
  const onKey = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, state.items.length - 1)); }
    if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
    if (e.key === "Enter" && state.items[active]) { e.preventDefault(); pick(state.items[active]); }
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="xs" disabled={disabled}>
          <Search /> {label || t("customs.item.override")}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[min(28rem,calc(100vw-24px))] p-0 surface-overlay rounded-xl"
        onOpenAutoFocus={(e) => { e.preventDefault(); inputRef.current?.focus(); }}>
        <div className="border-b border-[color:var(--border-subtle)] p-2">
          <input ref={inputRef} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onKey}
            placeholder={t("customs.hs.placeholder")} aria-label={t("customs.hs.placeholder")}
            className="surface-input h-8 w-full rounded-lg bg-transparent px-2.5 text-md md:text-sm outline-none placeholder:text-ink-tertiary" />
        </div>
        <ul role="listbox" className="max-h-72 overflow-y-auto p-1">
          {q.trim().length < 2 && <li className="px-2 py-3 text-sm text-ink-tertiary">{t("customs.hs.min")}</li>}
          {q.trim().length >= 2 && state.loading && !state.items.length && <li className="px-2 py-3 text-sm text-ink-tertiary">{t("customs.hs.searching")}</li>}
          {state.error && <li className="px-2 py-3 text-sm text-danger-fg">{state.error.message}</li>}
          {q.trim().length >= 2 && !state.loading && !state.error && !state.items.length && (
            <li className="px-2 py-3 text-sm text-ink-tertiary">{t("customs.hs.no_results")}</li>
          )}
          {state.items.map((it, i) => (
            <li key={it.code} role="option" aria-selected={i === active}>
              <button type="button" onMouseEnter={() => setActive(i)} onClick={() => pick(it)}
                className={cn("flex w-full items-start gap-3 rounded-lg px-2 py-1.5 text-left", i === active && "bg-hover")}>
                <span className="w-16 shrink-0 font-mono text-[13px] tabular">{fmtCode(it.code)}</span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm leading-5">{it.description}</span>
                  {it.heading_description && <span className="block truncate text-xs text-ink-tertiary">{it.heading_description}</span>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
