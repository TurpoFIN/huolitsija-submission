// Small presentation helpers shared by the V2 ops screens (quotes, carriers, rates, carrier selection).
import React, { useEffect, useState } from "react";
import { Clock, Layers, Plane, Ship, TrainFront, Truck, XCircle, CalendarClock } from "lucide-react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { StatusBadge } from "@/components/app/StatusBadge";
import { cbm, kg, money, num } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export const MODES = ["sea", "road", "air", "rail"];
export const MODE_ICON = { sea: Ship, road: Truck, air: Plane, rail: TrainFront, multimodal: Layers };
export const SERVICES = {
  sea: ["FCL", "LCL", "RORO"],
  road: ["ROAD_LTL", "ROAD_FTL"],
  air: ["AIR"],
  rail: ["RAIL_FCL", "RAIL_WAGON"],
};
// Typical pricing basis per service, used as the default when ops pick a service for a new rate card.
export const SERVICE_BASIS = { FCL: "PER_CONTAINER", LCL: "PER_WM", RORO: "PER_LDM", ROAD_LTL: "PER_LDM", ROAD_FTL: "PER_KM", AIR: "PER_KG", RAIL_FCL: "PER_CONTAINER", RAIL_WAGON: "PER_SHIPMENT" };
export const BASES = ["PER_CONTAINER", "PER_WM", "PER_KG", "PER_100KG", "PER_LDM", "PER_SHIPMENT", "PER_KM"];
export const SURCHARGE_BASES = [...BASES, "PER_BL", "PER_TEU"];
export const LINE_BASES = [...SURCHARGE_BASES, "PER_DECLARATION", "PER_FILING", "PERCENT"];
export const CONTAINER_TYPES = ["20GP", "40GP", "40HC", "45HC", "20RF", "40RF"];
export const RATE_SOURCES = ["indicative", "contract", "spot"];
export const INTEGRATION_TYPES = ["dcsa", "maersk", "dhl", "digitraffic", "partner_portal", "manual"];
// RULE V-1 (reduced Finnish rates are 13.5 % and 10 % since 2025).
export const VAT_CODES = ["FI_25_5", "FI_13_5", "FI_10", "ZERO_EXPORT", "ZERO_INTL_TRANSPORT", "EU_ICS_RC", "OUT_OF_SCOPE_DISBURSEMENT", "OUT_OF_SCOPE_FOREIGN"];
export const INCOTERMS = ["EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP"];

export function Dash() {
  return <span className="text-ink-tertiary">—</span>;
}

export const isBlank = (v) => v === null || v === undefined || v === "";

/** Money with cents always shown (line-level amounts). */
export const eur = (cents, locale) => money(cents, locale, { decimals: 2 });
/** Unit prices that may carry fractions of a cent (tier rates such as 0,135 €/kg). */
export const eurPrecise = (cents, locale) =>
  cents === null || cents === undefined || Number.isNaN(Number(cents))
    ? "—"
    : new Intl.NumberFormat(locale === "fi" ? "fi-FI" : "en-GB", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(cents / 100);
/** Money for totals: whole euros above 10 000 €. */
export const eurTotal = (cents, locale) => money(cents, locale);

export function ModeIcon({ mode, className }) {
  const { t } = useT();
  const Icon = MODE_ICON[mode];
  if (!Icon) return <Dash />;
  const label = tEnum(t, "mode", mode);
  return <Icon className={cn("size-4 shrink-0 text-ink-secondary", className)} aria-label={label} role="img"><title>{label}</title></Icon>;
}

export function ModeLabel({ mode, service, className }) {
  const { t } = useT();
  if (!mode) return <Dash />;
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", className)}>
      <ModeIcon mode={mode} />
      <span>{tEnum(t, "mode", mode)}</span>
      {service && <span className="text-ink-tertiary">· {t(`oq.service.${service}`) === `oq.service.${service}` ? service : t(`oq.service.${service}`)}</span>}
    </span>
  );
}

export function ModeIcons({ modes }) {
  if (!modes || modes.length === 0) return <Dash />;
  return (
    <span className="inline-flex items-center gap-1.5">
      {modes.map((m) => <ModeIcon key={m} mode={m} />)}
    </span>
  );
}

/** Short code for a location or rate-card end: LOCODE, else country list, else name. */
export function locCode(loc) {
  if (!loc) return null;
  if (loc.unlocode) return loc.unlocode;
  if (Array.isArray(loc.countries) && loc.countries.length) return loc.countries.join("/");
  if (loc.country) return loc.country;
  if (loc.region) return loc.region;
  return loc.name || loc.query || null;
}

export function locName(loc) {
  if (!loc) return null;
  return loc.name || loc.label || loc.query || null;
}

/** Lane: "CNSHA → FIHEL" in mono with names underneath (optional). */
export function Lane({ origin, destination, via, showNames = true, className }) {
  const { t } = useT();
  const o = locCode(origin);
  const d = locCode(destination);
  const oName = locName(origin);
  const dName = locName(destination);
  return (
    <span className={cn("flex min-w-0 flex-col", className)}>
      <span className="whitespace-nowrap font-mono text-xs">
        {o || "—"} <span className="text-ink-tertiary">→</span> {d || "—"}
        {via && via.length > 0 && <span className="ml-1.5 font-sans text-xs text-ink-tertiary">{t("oq.via")} {via.join(", ")}</span>}
      </span>
      {showNames && (oName || dName) && (
        <span className="truncate text-xs text-ink-tertiary" title={`${oName || "—"} → ${dName || "—"}`}>
          {oName && oName !== o ? oName : ""}{(oName && oName !== o) || (dName && dName !== d) ? " → " : ""}{dName && dName !== d ? dName : ""}
          {origin?.radius_km ? ` · ${t("oq.radius", { km: origin.radius_km })}` : ""}
        </span>
      )}
    </span>
  );
}

/** Tooltip with its own provider (the app shell does not mount one). */
export function Tip({ content, children, side = "top" }) {
  if (!content) return children;
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent side={side} className="max-w-xs whitespace-pre-line">{content}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

/** Neutral provenance pill: where a price comes from (indicative / contract / spot / manual). */
export function SourcePill({ source, className }) {
  const { t } = useT();
  if (!source) return <Dash />;
  return (
    <Tip content={t(`oq.source_hint.${source}`) === `oq.source_hint.${source}` ? null : t(`oq.source_hint.${source}`)}>
      <span tabIndex={0} className={cn("inline-flex h-5 items-center whitespace-nowrap rounded-full bg-secondary px-2 text-[11px] font-medium text-ink-secondary shadow-[inset_0_0_0_0.5px_var(--border-default)]", className)}>
        {t(`oq.source.${source}`)}
      </span>
    </Tip>
  );
}

/** Validity state for rate cards: text + glyph. valid stays quiet. */
export function RateState({ state, daysLeft, validFrom, className }) {
  const { t, locale } = useT();
  if (state === "expired") {
    return <span className={cn("inline-flex items-center gap-1 whitespace-nowrap text-xs font-medium text-danger-fg", className)}><XCircle className="size-3.5" aria-hidden />{t("oq.rate_state.expired")}</span>;
  }
  if (state === "expiring") {
    return (
      <span className={cn("inline-flex items-center gap-1 whitespace-nowrap text-xs font-medium text-warning-fg", className)}>
        <Clock className="size-3.5" aria-hidden />
        {isBlank(daysLeft) ? t("oq.rate_state.expiring") : t("oq.rate_state.expires_in", { n: num(daysLeft, locale) })}
      </span>
    );
  }
  if (state === "upcoming") {
    return <span className={cn("inline-flex items-center gap-1 whitespace-nowrap text-xs text-ink-secondary", className)}><CalendarClock className="size-3.5" aria-hidden />{t("oq.rate_state.upcoming")}</span>;
  }
  if (state === "valid") return <span className={cn("text-xs text-ink-tertiary", className)}>{t("oq.rate_state.valid")}</span>;
  return <Dash />;
}

const INTEGRATION_STATE = { live: "success", sandbox: "in-progress", not_configured: "expired" };

export function IntegrationBadge({ status, type, size = "sm", showType = false }) {
  const { t } = useT();
  if (!status) return <Dash />;
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      {showType && type && <span className="text-sm">{t(`oq.integration.${type}`)}</span>}
      <StatusBadge size={size} state={INTEGRATION_STATE[status] || "expired"} value={status} label={t(`oq.integration_status.${status}`)} />
    </span>
  );
}

/** On-time % with its provenance (benchmark vs own shipments) as a small neutral mark. */
export function OnTime({ value, source, sample }) {
  const { t, locale } = useT();
  if (isBlank(value)) return <Dash />;
  const mark = source ? t(`oq.perf_source_short.${source}`) : null;
  const hint = source ? `${t(`oq.perf_source.${source}`)}${sample ? ` · ${t("oq.sample_n", { n: num(sample, locale) })}` : ""}` : null;
  return (
    <span className="inline-flex items-baseline justify-end gap-1.5 whitespace-nowrap">
      <span className="tabular">{num(value, locale, 1)} %</span>
      {mark && (
        <Tip content={hint}>
          <span tabIndex={0} className="text-[11px] text-ink-tertiary underline decoration-dotted underline-offset-2">{mark}</span>
        </Tip>
      )}
    </span>
  );
}

/** Parse a Finnish or English money string ("1 234,50", "1234.5", "€ 12") to cents. Returns null when blank, NaN when invalid. */
export function parseMoney(text, { fractionCents = false } = {}) {
  if (text === null || text === undefined) return null;
  const s = String(text).replace(/[\s  €]/g, "").replace(/eur/i, "");
  if (s === "") return null;
  let norm = s;
  if (s.includes(",") && s.includes(".")) norm = s.lastIndexOf(",") > s.lastIndexOf(".") ? s.replace(/\./g, "").replace(",", ".") : s.replace(/,/g, "");
  else norm = s.replace(",", ".");
  const v = Number(norm);
  if (!Number.isFinite(v)) return NaN;
  return fractionCents ? Math.round(v * 10000) / 100 : Math.round(v * 100);
}

export function parseNum(text) {
  if (text === null || text === undefined) return null;
  const s = String(text).replace(/[\s  ]/g, "").replace(",", ".");
  if (s === "") return null;
  const v = Number(s);
  return Number.isFinite(v) ? v : NaN;
}

const centsToText = (cents, locale, maxDigits = 2) =>
  isBlank(cents) ? "" : new Intl.NumberFormat(locale === "fi" ? "fi-FI" : "en-GB", { minimumFractionDigits: 2, maximumFractionDigits: maxDigits, useGrouping: false }).format(cents / 100);
const numToText = (v, locale) => (isBlank(v) ? "" : String(v).replace(".", locale === "fi" ? "," : "."));

/**
 * Text input bound to integer cents. Keeps the typed text while focused (no caret jumps),
 * reformats on blur. Invalid text is flagged with aria-invalid and does not change the value.
 */
export function MoneyInput({ cents, onChange, className, disabled, fractionCents = false, ...rest }) {
  const { locale } = useT();
  const digits = fractionCents ? 4 : 2;
  const [text, setText] = useState(centsToText(cents, locale, digits));
  const [focused, setFocused] = useState(false);
  const [bad, setBad] = useState(false);
  useEffect(() => {
    if (!focused) setText(centsToText(cents, locale, digits));
  }, [cents, locale, focused, digits]);
  return (
    <input
      inputMode="decimal"
      value={text}
      disabled={disabled}
      aria-invalid={bad || undefined}
      onFocus={() => setFocused(true)}
      onBlur={() => { setFocused(false); setBad(false); setText(centsToText(cents, locale, digits)); }}
      onChange={(e) => {
        setText(e.target.value);
        const v = parseMoney(e.target.value, { fractionCents });
        if (Number.isNaN(v)) { setBad(true); return; }
        setBad(false);
        onChange(v);
      }}
      className={cn("surface-input h-8 w-full min-w-0 rounded-lg bg-transparent px-2 text-right text-md tabular md:text-sm outline-none disabled:opacity-60 aria-[invalid=true]:shadow-[inset_0_0_0_1px_var(--danger-500)]", className)}
      {...rest}
    />
  );
}

/** Text input bound to a number (accepts comma decimals). */
export function NumInput({ value, onChange, className, align = "right", disabled, ...rest }) {
  const { locale } = useT();
  const [text, setText] = useState(numToText(value, locale));
  const [focused, setFocused] = useState(false);
  const [bad, setBad] = useState(false);
  useEffect(() => {
    if (!focused) setText(numToText(value, locale));
  }, [value, locale, focused]);
  return (
    <input
      inputMode="decimal"
      value={text}
      disabled={disabled}
      aria-invalid={bad || undefined}
      onFocus={() => setFocused(true)}
      onBlur={() => { setFocused(false); setBad(false); setText(numToText(value, locale)); }}
      onChange={(e) => {
        setText(e.target.value);
        const v = parseNum(e.target.value);
        if (Number.isNaN(v)) { setBad(true); return; }
        setBad(false);
        onChange(v);
      }}
      className={cn("surface-input h-8 w-full min-w-0 rounded-lg bg-transparent px-2 text-md tabular md:text-sm outline-none disabled:opacity-60 aria-[invalid=true]:shadow-[inset_0_0_0_1px_var(--danger-500)]", align === "right" && "text-right", className)}
      {...rest}
    />
  );
}

/** Native select styled like the 32px inputs: compact and fast inside dense tables. */
export function NativeSelect({ value, onChange, options, className, placeholder, disabled, ...rest }) {
  return (
    <select
      value={value ?? ""}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      className={cn("surface-input h-8 w-full min-w-0 rounded-lg bg-transparent px-2 text-md md:text-sm outline-none disabled:opacity-60", className)}
      {...rest}
    >
      {placeholder !== undefined && <option value="">{placeholder}</option>}
      {options.map((o) => (
        <option key={o.value} value={o.value} disabled={o.disabled}>{o.label}</option>
      ))}
    </select>
  );
}

/** Label + control stacked, used by the dialogs and the rate sheet. */
export function Field({ label, htmlFor, hint, children, className, error }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      {label && <label htmlFor={htmlFor} className="text-xs font-medium text-ink-secondary">{label}</label>}
      {children}
      {error ? <span className="text-xs text-danger-fg">{error}</span> : hint ? <span className="text-xs text-ink-tertiary">{hint}</span> : null}
    </div>
  );
}

/** Mobile list row (390 px): primary line + secondary line + right value. */
export function MobileRow({ onClick, primary, status, secondary, right, rightSub, dim }) {
  return (
    <li>
      <button type="button" onClick={onClick} className={cn("flex w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-hover", dim && "opacity-60")}>
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex min-w-0 items-center gap-2">
            <span className="truncate text-sm font-medium">{primary}</span>
            {status}
          </span>
          {secondary && <span className="truncate text-xs text-ink-tertiary">{secondary}</span>}
        </span>
        {(right || rightSub) && (
          <span className="flex shrink-0 flex-col items-end gap-0.5">
            {right && <span className="text-sm tabular">{right}</span>}
            {rightSub && <span className="text-xs text-ink-tertiary">{rightSub}</span>}
          </span>
        )}
      </button>
    </li>
  );
}

/** useDebounced(value, ms) for search boxes. */
export function useDebounced(value, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const h = setTimeout(() => setV(value), ms);
    return () => clearTimeout(h);
  }, [value, ms]);
  return v;
}

/** Media query hook (Tailwind md = 768 px). */
export function useIsNarrow(px = 768) {
  const get = () => (typeof window !== "undefined" ? window.innerWidth < px : false);
  const [narrow, setNarrow] = useState(get);
  useEffect(() => {
    const on = () => setNarrow(get());
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [px]);
  return narrow;
}

export function RateCounts({ stats, inline = false }) {
  const { t, locale } = useT();
  if (!stats) return <Dash />;
  if (!stats.rate_cards) return <span className="text-ink-tertiary">{t("oq.carriers.no_rates")}</span>;
  return (
    <span className={cn("flex whitespace-nowrap text-sm", inline ? "flex-row items-baseline gap-3" : "flex-col gap-0.5")}>
      <span><span className="tabular">{num(stats.valid || 0, locale)}</span> <span className="text-ink-tertiary">{t("oq.carriers.valid")}</span></span>
      {stats.expiring > 0 && (
        <span className="inline-flex items-center gap-1 text-xs font-medium text-warning-fg"><Clock className="size-3" aria-hidden />{t("oq.carriers.expiring_n", { n: num(stats.expiring, locale) })}</span>
      )}
      {stats.expired > 0 && <span className="text-xs text-ink-tertiary">{t("oq.carriers.expired_n", { n: num(stats.expired, locale) })}</span>}
    </span>
  );
}

/** "1.4, 2.1, 2.2, 3, 4.1, 8, 9" -> "1–4, 8, 9" (main classes, ranges), full list in a tooltip. */
export function compressDg(classes) {
  const mains = [...new Set((classes || []).map((c) => parseInt(String(c), 10)).filter((n) => !Number.isNaN(n)))].sort((a, b) => a - b);
  const parts = [];
  for (let i = 0; i < mains.length; i++) {
    let j = i;
    while (j + 1 < mains.length && mains[j + 1] === mains[j] + 1) j++;
    parts.push(j - i >= 2 ? `${mains[i]}–${mains[j]}` : mains.slice(i, j + 1).join(", "));
    i = j;
  }
  return parts.join(", ");
}

export function DgClasses({ classes, full = false }) {
  const { t } = useT();
  if (!classes || classes.length === 0) return <span className="text-ink-tertiary">{t("oq.dg.none")}</span>;
  if (full) return <span className="tabular">{classes.join(", ")}</span>;
  return (
    <Tip content={classes.join(", ")}>
      <span tabIndex={0} className="cursor-help whitespace-nowrap tabular">{compressDg(classes)}</span>
    </Tip>
  );
}

/** Localised cargo line ("12 × EUR-lava · 4 800 kg · 14 m³ · goods") from request.cargo. */
export function cargoText(c, t, locale) {
  if (!c) return null;
  const parts = [];
  if (c.load_type === "FCL" || (c.containers && c.containers.length)) {
    parts.push((c.containers || []).map((x) => `${x.count} × ${x.type}`).join(", "));
  } else if (c.packages) {
    parts.push(`${num(c.packages, locale)} × ${t(`oq.pkg.${c.package_type || "other"}`)}`);
  }
  if (!isBlank(c.weight_kg)) parts.push(kg(c.weight_kg, locale));
  if (!isBlank(c.volume_m3)) parts.push(cbm(c.volume_m3, locale));
  if (c.description) parts.push(c.description);
  return parts.filter(Boolean).join(" · ") || null;
}

