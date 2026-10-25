// Shared bits for the exceptions and claims screens: media/debounce hooks, split view, SLA/age cells,
// detection provenance, shipment picker.
import React, { useEffect, useState } from "react";
import { Search, X } from "lucide-react";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { AgentMark } from "@/components/app/StatusBadge";
import { useApi } from "@/lib/api";
import { date, dateTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { compactDuration } from "./text";

export function useMedia(query) {
  const [match, setMatch] = useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setMatch(m.matches);
    on();
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, [query]);
  return match;
}

/** Two panes from 1280 px up; below that the detail opens in a full-height sheet. */
export const useWide = () => useMedia("(min-width: 1280px)");

export function useDebounced(value, ms = 300) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

/** Tick every `ms` so relative times stay true without refetching. */
export function useNow(ms = 60000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

/** "1 234,50" / "1234.5" / "1 234 €" -> cents; null when empty; NaN when not a number. */
export function eurToCents(s) {
  if (s === null || s === undefined) return null;
  const clean = String(s).replace(/\s| |€|eur/gi, "").replace(",", ".");
  if (!clean) return null;
  if (!/^\d+(\.\d{0,2})?$/.test(clean)) return NaN;
  return Math.round(parseFloat(clean) * 100);
}

export const centsToInput = (cents) => (cents === null || cents === undefined ? "" : (cents / 100).toFixed(2).replace(".", ","));

export function SplitView({ wide, open, onClose, list, detail, title, paneClassName }) {
  if (wide) {
    return (
      <div className="grid grid-cols-[minmax(0,1fr)_460px] items-start gap-4">
        <div className="min-w-0">{list}</div>
        <aside aria-label={title}
          className={cn("sticky top-[64px] max-h-[calc(100vh-80px)] overflow-y-auto rounded-xl border border-[color:var(--border-subtle)] bg-surface", paneClassName)}>
          {detail}
        </aside>
      </div>
    );
  }
  return (
    <>
      {list}
      <Sheet open={!!open} onOpenChange={(o) => { if (!o) onClose(); }}>
        <SheetContent side="right" className="w-full max-w-full gap-0 overflow-y-auto p-0 sm:max-w-xl [&>button.absolute]:hidden">
          <SheetTitle className="sr-only">{title}</SheetTitle>
          <SheetDescription className="sr-only">{title}</SheetDescription>
          {open ? detail : null}
        </SheetContent>
      </Sheet>
    </>
  );
}

/** SLA time left / over. Danger tone once passed. */
export function SlaDue({ due, status, now, className }) {
  const { t } = useT();
  if (!due) return <span className="text-ink-tertiary">—</span>;
  if (status === "resolved") return <span className={cn("text-ink-tertiary", className)}>{t("exceptions.sla.met")}</span>;
  const diff = new Date(due).getTime() - (now || Date.now());
  const d = compactDuration(diff, t);
  return diff < 0
    ? <span className={cn("whitespace-nowrap font-medium text-danger-fg tabular", className)}>{t("exceptions.sla.over", { d })}</span>
    : <span className={cn("whitespace-nowrap tabular", diff < 3600000 && "text-warning-fg", className)}>{t("exceptions.sla.left", { d })}</span>;
}

export function Age({ at, now }) {
  const { t, locale } = useT();
  if (!at) return <span className="text-ink-tertiary">—</span>;
  return <time dateTime={at} title={dateTime(at, locale)} className="whitespace-nowrap tabular">{t("exceptions.age", { d: compactDuration((now || Date.now()) - new Date(at).getTime(), t) })}</time>;
}

/** Detection provenance: AgentMark for the AI agent, plain text otherwise. */
export function DetectedBy({ value }) {
  const { t } = useT();
  if (value === "ai") return <AgentMark title={t("exceptions.source.ai_title")} />;
  const k = `exceptions.source.${value}`;
  return <span className="text-xs text-ink-secondary">{t(k) === k ? value || "—" : t(k)}</span>;
}

function shipmentLine(s, t, locale) {
  const route = [s.origin?.name, s.destination?.name].filter(Boolean).join(" → ");
  const when = s.delivered_at ? date(s.delivered_at, locale) : s.eta ? `ETA ${date(s.eta, locale)}` : null;
  return [s.customer_name, route, tEnum(t, "status", s.status), when].filter(Boolean).join(" · ");
}

/**
 * Search-as-you-type shipment picker. `query` adds fixed params (e.g. "status=delivered").
 * value = selected shipment object or null.
 */
export function ShipmentPicker({ value, onChange, query = "", placeholder, emptyText, filter, autoFocus, id }) {
  const { t, locale } = useT();
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 250);
  const params = [query, dq ? `q=${encodeURIComponent(dq)}` : "", "limit=30"].filter(Boolean).join("&");
  const res = useApi(value ? null : `/shipments?${params}`);
  if (value) {
    return (
      <div className="flex items-center gap-2 rounded-lg px-2.5 py-1.5 shadow-[inset_0_0_0_0.5px_var(--border-default)]">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">{value.reference}</p>
          <p className="truncate text-xs text-ink-tertiary">{shipmentLine(value, t, locale)}</p>
        </div>
        <button type="button" onClick={() => onChange(null)} className="rounded-full p-1 hover:bg-hover" aria-label={t("exceptions.create.change")}>
          <X className="size-3.5" />
        </button>
      </div>
    );
  }
  const items = (res.data?.items || []).filter((s) => (filter ? filter(s) : true));
  return (
    <div className="flex flex-col gap-1">
      <label className="surface-input flex h-8 items-center gap-2 rounded-lg px-2.5">
        <Search className="size-4 text-ink-tertiary" aria-hidden />
        <input id={id} data-quiet-input autoFocus={autoFocus} value={q} onChange={(e) => setQ(e.target.value)}
          placeholder={placeholder} aria-label={placeholder}
          className="min-w-0 flex-1 bg-transparent text-md outline-none placeholder:text-ink-tertiary md:text-sm" />
      </label>
      <div className="max-h-56 overflow-y-auto rounded-lg shadow-[inset_0_0_0_0.5px_var(--border-subtle)]" role="listbox" aria-label={placeholder}>
        {res.loading && !res.data ? (
          <p className="px-2.5 py-2 text-sm text-ink-tertiary">{t("state.loading")}</p>
        ) : res.error && !res.data ? (
          <p className="px-2.5 py-2 text-sm text-danger-fg">{res.error.message}</p>
        ) : items.length === 0 ? (
          <p className="px-2.5 py-2 text-sm text-ink-tertiary">{emptyText}</p>
        ) : (
          items.map((s) => (
            <button key={s.id} type="button" role="option" aria-selected={false} onClick={() => onChange(s)}
              className="flex w-full flex-col items-start px-2.5 py-1.5 text-left hover:bg-hover">
              <span className="text-sm font-medium">{s.reference}</span>
              <span className="w-full truncate text-xs text-ink-tertiary">{shipmentLine(s, t, locale)}</span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

/** Small labelled field wrapper. */
export function Field({ label, htmlFor, hint, error, children, className }) {
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      {label && <label htmlFor={htmlFor} className="text-xs font-medium text-ink-secondary">{label}</label>}
      {children}
      {error ? <p className="text-xs text-danger-fg">{error}</p> : hint ? <p className="text-xs text-ink-tertiary">{hint}</p> : null}
    </div>
  );
}

/** Inline "Saved" confirmation that fades after a few seconds. */
export function SavedNote({ show, label }) {
  const { t } = useT();
  if (!show) return null;
  return <span role="status" className="text-xs font-medium text-success-fg">{label || t("action.saved")}</span>;
}

export function useFlag(ms = 3000) {
  const [on, setOn] = useState(false);
  useEffect(() => {
    if (!on) return undefined;
    const id = setTimeout(() => setOn(false), ms);
    return () => clearTimeout(id);
  }, [on, ms]);
  return [on, () => setOn(true), () => setOn(false)];
}
