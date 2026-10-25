// Finance (s13) UI pieces shared by the /ops/finance screens. Built only from the app kit; no new colours or meanings.
import React, { useEffect, useRef, useState } from "react";
import { NavLink } from "react-router-dom";
import { CheckCircle2, Download, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DateInput } from "@/components/ui/DateInput";
import { Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiPost, fileUrl } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { amount, eur, presetRange } from "./format";

// Finance values -> the nine workflow visuals (StatusBadge VISUAL). Labels come from enum.<group>.<value> in i18n/finance.js.
export const FIN_STATES = {
  carrier_invoice: { received: "submitted", extracting: "in-progress", needs_review: "in-review", matched: "scheduled",
    disputed: "flagged", approved: "success", scheduled: "scheduled", paid: "success", rejected: "failed", duplicate: "expired" },
  accrual: { open: "in-progress", no_booked_rate: "pending", matched: "success", partially_matched: "pending", disputed: "flagged", released: "expired",
    written_off: "expired" },
  payable: { approved: "scheduled", scheduled: "scheduled", sent: "submitted", paid: "success", rejected: "failed", cancelled: "expired" },
  payment_run: { draft: "expired", pending_approval: "in-review", approved: "scheduled", sent: "submitted", accepted: "success",
    rejected: "failed", partially_accepted: "pending" },
  collections: { final_demand: "pending", payment_plan: "scheduled", agency: "flagged", legal: "flagged", paid: "success",
    written_off: "expired", closed: "expired" },
  pnl_basis: { actual: "success", partly_actual: "in-progress", estimated: "expired" },
  ledger_export: { pending: "in-progress", running: "in-progress", done: "success", ready: "success", sent: "submitted",
    failed: "failed" },
  extraction: { pending: "in-progress", processing: "in-progress", done: "success", needs_review: "in-review", failed: "failed",
    unavailable: "in-review" },
};

export function FinStatus({ group, value, size = "sm" }) {
  const { t } = useT();
  if (value === null || value === undefined || value === "") return <span className="text-ink-tertiary">—</span>;
  return <StatusBadge size={size} state={FIN_STATES[group]?.[value] || "expired"} value={value} label={tEnum(t, `fin.${group}`, value)} />;
}

/** Right-aligned money cell. Shows the original currency, and EUR beneath it when they differ. */
export function MoneyCell({ cents, currency, eurCents, className, strong }) {
  const { locale } = useT();
  const foreign = currency && currency !== "EUR";
  return (
    <span className={cn("inline-flex flex-col items-end tabular whitespace-nowrap", className)}>
      <span className={cn(strong && "font-medium", (cents === null || cents === undefined) && "text-ink-tertiary")}>
        {foreign ? amount(cents, currency, locale) : eur(cents ?? eurCents, locale)}
      </span>
      {foreign && <span className="text-[11px] text-ink-tertiary">{eur(eurCents, locale)}</span>}
    </span>
  );
}

const TABS = [
  { to: "/ops/finance", key: "overview", end: true },
  { to: "/ops/finance/pnl", key: "pnl" },
  { to: "/ops/finance/accruals", key: "accruals" },
  { to: "/ops/finance/carrier-invoices", key: "carrier_invoices" },
  { to: "/ops/finance/payables", key: "payables" },
  { to: "/ops/finance/collections", key: "collections" },
  { to: "/ops/finance/fx", key: "fx" },
  { to: "/ops/finance/vat", key: "vat" },
  { to: "/ops/finance/ledger", key: "ledger" },
];
export const FINANCE_TABS = TABS;

/** Sub-navigation between the finance screens (links styled as the kit's Segmented control). */
export function FinanceTabs() {
  const { t } = useT();
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current?.querySelector('[aria-current="page"]');
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
  return (
    <nav ref={ref} aria-label={t("fin.subnav")}
      className="mb-4 inline-flex h-8 max-w-full items-center overflow-x-auto rounded-full bg-secondary p-[3px] no-scrollbar">
      {TABS.map((x) => (
        <NavLink key={x.key} to={x.to} end={x.end}
          className={({ isActive }) => cn("inline-flex h-[26px] shrink-0 items-center whitespace-nowrap rounded-full px-3 text-sm font-medium text-ink-secondary hover:text-foreground",
            isActive && "bg-surface text-foreground shadow-[inset_0_0_0_0.5px_var(--border-default)]")}>
          {t(`fin.tab.${x.key}`)}
        </NavLink>
      ))}
    </nav>
  );
}

/** Screen bar under the tabs: hidden heading for assistive tech, the decision line on the left, actions on the right. */
export function FinBar({ title, meta, actions, className }) {
  return (
    <div className={cn("mb-4 flex flex-wrap items-center justify-between gap-3", className)}>
      <h2 className="sr-only">{title}</h2>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm text-ink-secondary">{meta}</div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Dot separator for meta rows. */
export const Dot = () => <span aria-hidden className="text-ink-tertiary">·</span>;

/** Result line after an action (saved state). tone: "success" | "error". */
export function ActionNote({ note, onClose, className }) {
  const { t } = useT();
  if (!note) return null;
  if (note.tone === "error") return <ErrorState className={className} compact error={{ message: note.text }} />;
  return (
    <div role="status" className={cn("flex items-center gap-2 rounded-lg px-3 py-2 text-sm status-success", className)}>
      <CheckCircle2 className="size-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1">{note.text}</span>
      {onClose && (
        <button type="button" onClick={onClose} aria-label={t("action.close")} className="rounded-full p-0.5 hover:bg-hover">
          <X className="size-3.5" />
        </button>
      )}
    </div>
  );
}

/** POST helper with busy/error state: const run = usePost(); await run.post(path, body) -> response | null. */
export function usePost() {
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const post = async (path, body = {}, key = path) => {
    setBusy(key);
    setError(null);
    try {
      return await apiPost(path, body);
    } catch (e) {
      setError(e);
      return null;
    } finally {
      setBusy(null);
    }
  };
  return { busy, error, post, setError };
}

/**
 * Confirm dialog for a write action, with an optional reason (required for dispute/reject/write-off).
 * onSubmit(reason) -> Promise<boolean>; closes when it resolves true.
 */
export function ConfirmAction({ open, onOpenChange, title, description, submitLabel, reason, reasonRequired, destructive, onSubmit, children }) {
  const { t } = useT();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (open) {
      setText("");
      setError(null);
    }
  }, [open]);
  const missing = reasonRequired && !text.trim();
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const ok = await onSubmit(text.trim());
      if (ok !== false) onOpenChange(false);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        {children}
        {reason && (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="fin-reason">{reasonRequired ? t("fin.act.reason_required") : t("fin.act.reason")}</Label>
            <Textarea id="fin-reason" value={text} onChange={(e) => setText(e.target.value)} className="min-h-[72px]" />
          </div>
        )}
        {error && <ErrorState compact error={error} />}
        <DialogFooter className="flex-col gap-2 sm:flex-row sm:items-center">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>{t("action.cancel")}</Button>
          <Button variant={destructive ? "destructive" : "default"} onClick={submit} disabled={busy || missing}>
            {busy ? t("state.saving") : submitLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Plain download link for an API file (browser request, cookie auth). */
export function FileLink({ path, label, size = "xs" }) {
  const { t } = useT();
  return (
    <Button asChild size={size} variant="outline">
      <a href={fileUrl(path)} target="_blank" rel="noreferrer"><Download /> {label || t("action.download")}</a>
    </Button>
  );
}

const PRESETS = ["mtd", "last_month", "qtd", "ytd"];

/** Report period: presets as pills + custom from/to dates. value {from, to, preset}; onChange(next). */
export function PeriodFilter({ value, onChange }) {
  const { t } = useT();
  const preset = value.preset || "custom";
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Segmented label={t("fin.period.label")} value={preset}
        onChange={(p) => (p === "custom" ? onChange({ ...value, preset: "custom" }) : onChange({ ...presetRange(p), preset: p }))}
        options={[...PRESETS.map((p) => ({ value: p, label: t(`fin.period.${p}`) })), { value: "custom", label: t("fin.period.custom") }]} />
      {preset === "custom" && (
        <span className="flex flex-wrap items-center gap-2">
          <span className="w-36"><DateInput value={value.from || ""} onChange={(v) => onChange({ ...value, from: v, preset: "custom" })} aria-label={t("fin.period.from")} /></span>
          <span className="text-ink-tertiary" aria-hidden>–</span>
          <span className="w-36"><DateInput value={value.to || ""} onChange={(v) => onChange({ ...value, to: v, preset: "custom" })} aria-label={t("fin.period.to")} /></span>
        </span>
      )}
    </div>
  );
}

/** Month picker for period-based reports (VAT, revaluation): previous / label / next. */
export function MonthStepper({ period, onChange, max }) {
  const { t, locale } = useT();
  const [y, m] = period.split("-").map(Number);
  const shift = (n) => {
    const d = new Date(Date.UTC(y, m - 1 + n, 1));
    return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
  };
  const label = new Date(Date.UTC(y, m - 1, 15)).toLocaleDateString(locale === "fi" ? "fi-FI" : "en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
  return (
    <span className="inline-flex items-center gap-1" role="group" aria-label={t("fin.period.label")}>
      <Button size="icon-sm" variant="outline" onClick={() => onChange(shift(-1))} aria-label={t("fin.period.prev")}>‹</Button>
      <span className="min-w-[9rem] text-center text-sm font-medium tabular">{label}</span>
      <Button size="icon-sm" variant="outline" onClick={() => onChange(shift(1))} disabled={max && shift(1) > max} aria-label={t("fin.period.next")}>›</Button>
    </span>
  );
}
