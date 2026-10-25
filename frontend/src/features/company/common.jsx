// Shared bits for the company / KYC screens (portal Company, ops Customers).
import React, { useEffect, useState } from "react";
import { ExternalLink } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/app/StatusBadge";
import { date, dateTime, daysUntil } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

// KYC check result -> one of the nine workflow states.
export const CHECK_STATE = { pass: "success", warn: "pending", fail: "failed", error: "flagged", not_applicable: "expired", not_run: "expired",
  in_review: "in-review", manual: "expired", review: "in-review" };
export const SANCTIONS_STATE = { clear: "success", potential_match: "flagged", blocked: "failed", not_screened: "expired", error: "flagged",
  in_review: "in-review" };

/** Check summary in the UI language (backend sends summary_fi next to the English summary). */
export function checkSummary(c, locale) {
  return (locale === "fi" && c?.summary_fi) || c?.summary || null;
}
export const RISK_CHECKS = ["ytunnus", "ytj", "vies", "sanctions", "ubo", "pep"];
export const CUSTOMS_CHECKS = ["eori", "poa"];

export function CheckBadge({ status, running, audience, size = "sm" }) {
  const { t } = useT();
  if (running) return <StatusBadge size={size} state="in-progress" label={t("company.cs.running")} />;
  const label = status === "manual" && audience === "ops" ? t("company.cs.manual_ops") : t(`company.cs.${status || "not_run"}`);
  return <StatusBadge size={size} state={CHECK_STATE[status] || "expired"} label={label} />;
}

export function SanctionsBadge({ value, size = "sm", quietClear }) {
  const { t } = useT();
  const v = value || "not_screened";
  if (quietClear && v === "clear") return <span className="text-xs text-ink-tertiary">{tEnum(t, "sanctions", v)}</span>;
  return <StatusBadge size={size} state={SANCTIONS_STATE[v] || "expired"} label={tEnum(t, "sanctions", v)} />;
}

/** POA validity: valid (with expiry warning under 30 days), expired, revoked or missing. */
export function poaState(poa, poaValid) {
  if (!poa || !poa.signed_at) return "missing";
  if (poa.revoked_at) return "revoked";
  if (poaValid) return "valid";
  const today = new Date().toISOString().slice(0, 10);
  if (poa.valid_from && poa.valid_from > today) return "scheduled";
  return "expired";
}

export function PoaBadge({ poa, valid, validTo, size = "sm" }) {
  const { t, locale } = useT();
  const state = poa ? poaState(poa, valid) : valid ? "valid" : "missing";
  const to = validTo || poa?.valid_to;
  if (state === "valid") {
    const d = daysUntil(to);
    if (d !== null && d <= 30) return <StatusBadge size={size} state="pending" label={t("company.poa.expires_in", { days: d })} />;
    return <StatusBadge size={size} state="success" label={t("company.poa.valid_to_short", { date: date(to, locale) })} />;
  }
  const map = { missing: "pending", revoked: "expired", expired: "expired", scheduled: "scheduled" };
  return <StatusBadge size={size} state={map[state]} label={t(`company.poa.${state}`)} />;
}

export function DemoTag({ className }) {
  const { t } = useT();
  return <Badge variant="outline" className={className} title={t("customers.demo_hint")}>{t("customers.demo")}</Badge>;
}

/** "Source: YTJ 24 Oct 2026" provenance label, linked to the register entry when we have the URL. */
export function SourceLabel({ source, at, url, withTime, label, className }) {
  const { locale } = useT();
  const text = label || [source, at ? (withTime ? dateTime(at, locale) : date(at, locale)) : null].filter(Boolean).join(" · ");
  if (!text) return null;
  const body = (
    <span className={cn("inline-flex items-center gap-1 text-xs text-ink-tertiary", url && "hover:text-foreground", className)}>
      {text}
      {url && <ExternalLink className="size-3" aria-hidden />}
    </span>
  );
  return url ? <a href={url} target="_blank" rel="noreferrer" className="min-w-0">{body}</a> : body;
}

/** Short register name for a check source string. */
export function shortSource(source) {
  if (!source) return null;
  if (/YTJ/i.test(source)) return "YTJ";
  if (/VIES/i.test(source)) return "VIES";
  if (/EORI|EOS/i.test(source)) return "EU EOS";
  if (/sanction|OFAC|UN /i.test(source)) return "EU · UN · OFAC";
  if (/check digit|manual|verified by/i.test(source)) return null;
  return source;
}

export function actorName(a) {
  if (!a) return null;
  if (typeof a === "string") return a;
  return a.name || a.email || a.id || null;
}

export function useIsNarrow(px = 767) {
  const q = `(max-width: ${px}px)`;
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.matchMedia(q).matches);
  useEffect(() => {
    const m = window.matchMedia(q);
    const on = () => setNarrow(m.matches);
    on();
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

/** Finnish Business ID: 1234567-8, check digit with weights 7,9,10,5,8,4,2 mod 11. */
export function normaliseBusinessId(s) {
  const raw = String(s || "").replace(/\s/g, "");
  const m = raw.match(/^(\d{6,7})-?(\d)$/);
  if (!m) return null;
  return `${m[1].padStart(7, "0")}-${m[2]}`;
}

export function ytunnusValid(s) {
  const bid = normaliseBusinessId(s);
  if (!bid) return false;
  const w = [7, 9, 10, 5, 8, 4, 2];
  const r = bid.slice(0, 7).split("").reduce((a, d, i) => a + Number(d) * w[i], 0) % 11;
  if (r === 1) return false;
  return (r === 0 ? 0 : 11 - r) === Number(bid[8]);
}

/** "50 000,00" / "50000.5" / "50 000 €" -> cents; "" -> null; garbage -> NaN. */
export function parseEuro(s) {
  if (s === null || s === undefined) return null;
  const clean = String(s).replace(/[\s €]/g, "").replace(",", ".");
  if (clean === "") return null;
  if (!/^\d+(\.\d{0,2})?$/.test(clean)) return NaN;
  return Math.round(parseFloat(clean) * 100);
}

export function centsToInput(c) {
  if (c === null || c === undefined) return "";
  return (c / 100).toFixed(c % 100 === 0 ? 0 : 2);
}

/** Labelled form field with optional hint, provenance and inline error. */
export function Field({ label, htmlFor, hint, error, source, children, className }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      <div className="flex min-h-4 items-baseline justify-between gap-2">
        <label htmlFor={htmlFor} className="text-xs text-ink-secondary">{label}</label>
        {source}
      </div>
      {children}
      {error ? <p className="text-xs text-danger-fg" role="alert">{error}</p> : hint ? <p className="text-xs text-ink-tertiary">{hint}</p> : null}
    </div>
  );
}
