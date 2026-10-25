// Shared customs UI helpers: status groups, SLA age, flags, provenance line, small formatting helpers.
import React, { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Clock, Flag, XCircle } from "lucide-react";
import { AgentMark } from "@/components/app/StatusBadge";
import { useT } from "@/lib/i18n";
import { dateTime, num } from "@/lib/format";
import { cn } from "@/lib/utils";

export const GROUPS = [
  { key: "review", statuses: ["ready_for_review"] },
  { key: "drafts", statuses: ["draft", "needs_info"] },
  { key: "filing", statuses: ["approved", "submitted", "accepted"] },
  { key: "control", statuses: ["under_control", "rejected", "not_released"] },
  { key: "released", statuses: ["released", "closed"] },
  { key: "all", statuses: null },
];

export const PORTAL_GROUPS = [
  { key: "input", statuses: ["draft", "needs_info"] },
  { key: "progress", statuses: ["ready_for_review", "approved", "submitted", "accepted", "under_control", "rejected"] },
  { key: "released", statuses: ["released", "closed", "not_released", "invalidated"] },
  { key: "all", statuses: null },
];

export function inGroup(groups, key, status) {
  const g = groups.find((x) => x.key === key);
  return !g || !g.statuses || g.statuses.includes(status);
}

export function groupCount(groups, key, counts = {}) {
  const g = groups.find((x) => x.key === key);
  if (!g) return 0;
  if (!g.statuses) return Object.values(counts).reduce((a, b) => a + b, 0);
  return g.statuses.reduce((a, s) => a + (counts[s] || 0), 0);
}

// Hours a declaration may sit in a state before it is at risk / breached (internal working targets).
const SLA = {
  draft: [48, 96], needs_info: [48, 96], ready_for_review: [24, 48], approved: [4, 24],
  submitted: [24, 72], accepted: [24, 72], under_control: [24, 72], rejected: [8, 24],
};

export function slaState(status, since) {
  if (!since) return { hours: null, state: "none" };
  const hours = (Date.now() - new Date(since).getTime()) / 3600000;
  const lim = SLA[status];
  if (!lim) return { hours, state: "none" };
  return { hours, state: hours >= lim[1] ? "breached" : hours >= lim[0] ? "at_risk" : "ok" };
}

export function ageLabel(hours, t) {
  if (hours === null || hours === undefined) return "—";
  if (hours < 1) return t("customs.age.min", { n: Math.max(1, Math.round(hours * 60)) });
  if (hours < 48) return t("customs.age.h", { n: Math.round(hours) });
  return t("customs.age.d", { n: Math.round(hours / 24) });
}

/** SLA age: text + glyph. Breached uses the flagged (pink) state: red stays reserved for holds. */
export function SlaAge({ status, since }) {
  const { t } = useT();
  const { hours, state } = slaState(status, since);
  const Icon = state === "breached" ? Flag : state === "at_risk" ? AlertTriangle : Clock;
  return (
    <span className={cn("inline-flex items-center gap-1 whitespace-nowrap tabular text-sm",
      state === "breached" && "text-[color:var(--status-flagged-fg)]",
      state === "at_risk" && "text-[color:var(--status-pending-fg)]",
      (state === "ok" || state === "none") && "text-ink-secondary")}>
      <Icon className="size-3.5" aria-hidden />
      {ageLabel(hours, t)}
      {state === "at_risk" && <span className="sr-only md:not-sr-only text-xs">· {t("customs.sla.at_risk")}</span>}
      {state === "breached" && <span className="sr-only md:not-sr-only text-xs">· {t("customs.sla.breached")}</span>}
    </span>
  );
}

const FLAG_TONE = { cbam: "status-pending", dual_use: "status-in-review", ru_by_origin: "status-failed", military: "status-failed",
  invalid_code: "status-flagged", trade_defence: "status-pending" };

export function FlagChips({ flags = [], className }) {
  const { t } = useT();
  const kinds = [...new Set(flags.map((f) => (typeof f === "string" ? f : f.kind)).filter(Boolean))];
  if (!kinds.length) return null;
  return (
    <span className={cn("inline-flex flex-wrap gap-1", className)}>
      {kinds.map((k) => {
        const f = flags.find((x) => (typeof x === "string" ? x : x.kind) === k);
        return (
          <span key={k} title={typeof f === "object" ? f.message : undefined}
            className={cn("inline-flex h-5 items-center rounded-full px-1.5 text-[11px] font-medium", FLAG_TONE[k] || "status-expired")}>
            {t(`customs.flag.${k}`) === `customs.flag.${k}` ? k.replace(/_/g, " ") : t(`customs.flag.${k}`)}
          </span>
        );
      })}
    </span>
  );
}

/** "AI drafted · broker approves before filing" with the violet provenance mark. */
export function ProvenanceLine({ decl, className }) {
  const { t, locale } = useT();
  if (!decl?.ai) return null;
  const by = decl.ai.drafted_by?.name;
  const model = decl.ai.hs_model;
  return (
    <p className={cn("text-sm leading-6 text-ink-secondary", className)}>
      <AgentMark title={t("ai.drafted")} className="mr-2 align-[1px]" />
      <span className="mr-2">{t("customs.provenance")}</span>
      <span className="text-xs text-ink-tertiary">
        {[by, decl.ai.drafted_at ? dateTime(decl.ai.drafted_at, locale) : null, model].filter(Boolean).join(" · ")}
      </span>
    </p>
  );
}

export function RuleChip({ rule }) {
  return (
    <span className="inline-flex h-5 shrink-0 items-center rounded-md bg-secondary px-1.5 font-mono text-[11px] text-ink-secondary">{rule}</span>
  );
}

export function OkGlyph({ ok, severity }) {
  if (ok) return <CheckCircle2 className="size-4 shrink-0 text-[color:var(--status-success-fg)]" aria-hidden />;
  if (severity === "advisory") return <AlertTriangle className="size-4 shrink-0 text-[color:var(--status-pending-fg)]" aria-hidden />;
  return <XCircle className="size-4 shrink-0 text-[color:var(--status-failed-fg)]" aria-hidden />;
}

export const kgFmt = (v, locale) => (v === null || v === undefined ? "—" : `${num(v, locale, v < 10 ? 2 : 1)} kg`);

export function fmtCode(code) {
  if (!code) return "—";
  const d = String(code);
  if (d.length === 8) return `${d.slice(0, 4)} ${d.slice(4, 6)} ${d.slice(6)}`;
  if (d.length === 6) return `${d.slice(0, 4)} ${d.slice(4)}`;
  if (d.length === 10) return `${d.slice(0, 4)} ${d.slice(4, 6)} ${d.slice(6, 8)} ${d.slice(8)}`;
  return d;
}

export function pctFmt(v, locale) {
  if (v === null || v === undefined) return "—";
  return `${num(v, locale, 1)} %`;
}

export function invoiceMoney(amount, currency, locale) {
  if (amount === null || amount === undefined) return "—";
  try {
    return new Intl.NumberFormat(locale === "fi" ? "fi-FI" : "en-GB", { style: "currency", currency: currency || "EUR",
      minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(amount);
  } catch (e) {
    return `${num(amount, locale, 2)} ${currency || ""}`;
  }
}

export function useIsNarrow(px = 768) {
  const q = `(max-width: ${px - 1}px)`;
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.matchMedia(q).matches);
  useEffect(() => {
    const m = window.matchMedia(q);
    const on = () => setNarrow(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, [q]);
  return narrow;
}

export function useDebounced(value, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

/** Parse a user-entered EUR amount ("1 234,50") to cents; null when empty, NaN when invalid. */
export function eurToCents(s) {
  if (s === null || s === undefined || String(s).trim() === "") return null;
  const n = Number(String(s).replace(/\s| /g, "").replace(",", "."));
  return Number.isFinite(n) ? Math.round(n * 100) : NaN;
}

export function parseNum(s) {
  if (s === null || s === undefined || String(s).trim() === "") return null;
  const n = Number(String(s).replace(/\s| /g, "").replace(",", "."));
  return Number.isFinite(n) ? n : NaN;
}

export function centsToInput(c) {
  if (c === null || c === undefined) return "";
  return (c / 100).toFixed(2);
}

/** Inline "Saved" / "Saving…" acknowledgement next to an edited block. */
export function SaveState({ state }) {
  const { t } = useT();
  if (!state) return null;
  return (
    <span className={cn("text-xs", state === "error" ? "text-danger-fg" : "text-ink-tertiary")} aria-live="polite">
      {state === "saving" ? t("state.saving") : state === "saved" ? t("action.saved") : t("customs.save_failed")}
    </span>
  );
}
