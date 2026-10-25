// Editable price lines for one quote option (ops). Live cost / sell / margin; markup % reprices cost lines.
// Draft shape: { id, lines: [Line + _k], markup: number|null, mode: "kept"|"set"|"manual", dirty }
import React from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ChevronRight, Info, Plus, Star, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { num } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { displayText } from "@/lib/localizedData";
import { cn } from "@/lib/utils";
import { LINE_BASES, MoneyInput, NativeSelect, NumInput, SourcePill, VAT_CODES, eur, eurTotal, isBlank, ModeIcon, Tip } from "./common";
import { applyMargin, emptyLine, lineAmount, optionTotals, priceForMargin } from "./quoteMath";
import { warningText } from "@/features/quotes/quoteFormat";
import { basisLabel, dateNum } from "./RatesTable";

let keySeq = 0;
export const withKeys = (lines) => (lines || []).map((l) => ({ ...l, _k: `l${++keySeq}` }));

export function initDraft(option) {
  return { id: option.id, lines: withKeys(option.lines), markup: option.markup_pct ?? null, mode: "kept", dirty: false };
}

export function hasDgLine(lines) {
  return (lines || []).some((l) => (l.code || "").toUpperCase() === "DG" || (l.code || "").toUpperCase().startsWith("DG_"));
}

/** Warning list: translated by code (quotes.warn.<code> with params), falls back to the backend's English text. */
export function Warnings({ items, className }) {
  const { t, locale } = useT();
  if (!items || items.length === 0) return null;
  return (
    <ul className={cn("flex flex-col gap-1", className)}>
      {items.map((w, i) => {
        const text = warningText(t, w, locale);
        const Icon = w.level === "info" ? Info : AlertTriangle;
        return (
          <li key={`${w.code}-${i}`} className={cn("flex items-start gap-1.5 text-sm",
            w.level === "error" ? "text-danger-fg" : w.level === "warning" ? "text-warning-fg" : "text-ink-secondary")}>
            <Icon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            <span>{text}</span>
          </li>
        );
      })}
    </ul>
  );
}

function descOf(l, locale) {
  return locale === "fi" && l.description_fi ? l.description_fi : l.description;
}

function noteOf(l, locale) {
  return locale === "fi" && l.note_fi ? l.note_fi : l.note;
}

function LineRow({ l, onChange, onRemove, readOnly, locale, t }) {
  const amount = lineAmount(l);
  if (readOnly) {
    return (
      <tr className="border-b border-[color:var(--border-subtle)]">
        <td className="px-2 py-1.5 text-xs text-ink-tertiary">{t(`review.charge.${l.code}`) === `review.charge.${l.code}` ? descOf(l, locale) || l.code : t(`review.charge.${l.code}`)}</td>
        <td className="px-2 py-1.5">{descOf(l, locale) || "—"}{noteOf(l, locale) && <span className="ml-1.5 text-xs text-ink-tertiary">{noteOf(l, locale)}</span>}{l.estimated && <span className="ml-1.5 text-xs text-ink-tertiary">{t("oq.line.estimated")}</span>}</td>
        <td className="px-2 py-1.5 text-ink-secondary">{basisLabel(t, l.basis) || "—"}</td>
        <td className="px-2 py-1.5 text-right tabular">{num(l.qty, locale, 2)}</td>
        <td className="px-2 py-1.5 text-right tabular text-ink-secondary">{eur(l.unit_cost_cents, locale)}</td>
        <td className="px-2 py-1.5 text-right tabular">{eur(l.unit_price_cents, locale)}</td>
        <td className="px-2 py-1.5 text-right tabular font-medium">{eur(amount, locale)}</td>
        <td className="px-2 py-1.5 text-xs">{l.vat_code ? t(`oq.vat.${l.vat_code}`) : "—"}</td>
        <td className="px-2 py-1.5 text-xs text-ink-secondary">{l.is_pass_through ? t("review.invoice.pass_through") : t("review.invoice.service")}</td>
        <td />
      </tr>
    );
  }
  return (
    <tr className="border-b border-[color:var(--border-subtle)] align-middle">
      <td className="px-1 py-1"><input aria-label={t("oq.line.code")} value={l.code || ""} onChange={(e) => onChange({ code: e.target.value.toUpperCase() })}
        className="surface-input h-8 w-full rounded-lg bg-transparent px-2 font-mono text-[13px] uppercase outline-none" /></td>
      <td className="px-1 py-1">
        <span className="flex items-center gap-2">
          <input aria-label={t("oq.line.description")} value={descOf(l, locale) || ""}
            onChange={(e) => onChange(locale === "fi" && l.description_fi ? { description_fi: e.target.value } : { description: e.target.value })}
            className="surface-input h-8 min-w-0 flex-1 rounded-lg bg-transparent px-2 text-md outline-none md:text-sm" />
          {(noteOf(l, locale) || l.estimated) && <span className="max-w-[110px] shrink-0 truncate text-[11px] text-ink-tertiary" title={noteOf(l, locale) || ""}>{l.estimated ? t("oq.line.estimated") : noteOf(l, locale)}</span>}
        </span>
      </td>
      <td className="px-1 py-1">
        <NativeSelect aria-label={t("oq.rates.col.basis")} value={l.basis || "PER_SHIPMENT"} onChange={(v) => onChange({ basis: v })}
          options={[...LINE_BASES, ...(l.basis && !LINE_BASES.includes(l.basis) ? [l.basis] : [])].map((b) => ({ value: b, label: basisLabel(t, b) }))} />
      </td>
      <td className="px-1 py-1"><NumInput aria-label={t("oq.line.qty")} value={l.qty} onChange={(v) => onChange({ qty: v ?? 0 })} /></td>
      <td className="px-1 py-1"><MoneyInput aria-label={t("oq.line.unit_cost")} cents={l.unit_cost_cents} onChange={(v) => onChange({ unit_cost_cents: v ?? 0 }, "cost")} /></td>
      <td className="px-1 py-1">
        <MoneyInput aria-label={t("oq.line.unit_price")} cents={l.unit_price_cents} disabled={l.is_pass_through}
          title={l.is_pass_through ? t("oq.line.pass_price_locked") : undefined} onChange={(v) => onChange({ unit_price_cents: v ?? 0 }, "price")} />
      </td>
      <td className="px-2 py-1 text-right tabular font-medium whitespace-nowrap">{eur(amount, locale)}</td>
      <td className="px-1 py-1">
        <NativeSelect aria-label={t("oq.line.vat")} value={l.vat_code} placeholder="—" onChange={(v) => onChange({ vat_code: v })}
          options={[...VAT_CODES, ...(l.vat_code && !VAT_CODES.includes(l.vat_code) ? [l.vat_code] : [])].map((c) => ({ value: c, label: t(`oq.vat.${c}`) === `oq.vat.${c}` ? c : t(`oq.vat.${c}`) }))} />
      </td>
      <td className="px-2 py-1 text-center">
        <Checkbox aria-label={t("oq.line.pass")} checked={!!l.is_pass_through} onCheckedChange={(v) => onChange({ is_pass_through: !!v }, "pass")} />
      </td>
      <td className="px-1 py-1">
        <Button size="icon-sm" variant="ghost" aria-label={t("oq.line.remove")} onClick={onRemove}><X /></Button>
      </td>
    </tr>
  );
}

/** Lines table + totals. Used for existing options and the manual option. */
export function LinesEditor({ draft, onDraft, readOnly, dgRequired }) {
  const { t, locale } = useT();
  const lines = draft.lines;
  const totals = optionTotals(lines);

  const update = (k, patch, kind) => {
    const next = lines.map((l) => {
      if (l._k !== k) return l;
      let nl = { ...l, ...patch, _edited: true };
      if (nl.is_pass_through) nl.unit_price_cents = nl.unit_cost_cents;
      else if (kind === "pass" && !nl.is_pass_through && draft.markup !== null && draft.mode !== "manual" && nl.unit_cost_cents > 0) {
        nl.unit_price_cents = priceForMargin(nl.unit_cost_cents, draft.markup);
      } else if (kind === "cost" && draft.markup !== null && draft.mode !== "manual" && nl.unit_cost_cents > 0) {
        nl.unit_price_cents = priceForMargin(nl.unit_cost_cents, draft.markup);
      }
      return nl;
    });
    onDraft({ ...draft, lines: next, dirty: true, ...(kind === "price" ? { mode: "manual", markup: null } : {}) });
  };
  const remove = (k) => onDraft({ ...draft, lines: lines.filter((l) => l._k !== k), dirty: true });
  const defaultVat = lines.find((l) => !l.is_pass_through && l.vat_code)?.vat_code || "FI_25_5";
  const add = (preset) => onDraft({ ...draft, lines: [...lines, ...withKeys([{ ...emptyLine(defaultVat), ...(preset || {}) }])], dirty: true });
  const setMarkup = (m) => {
    if (m === null) { onDraft({ ...draft, markup: null, mode: "manual", dirty: true }); return; }
    onDraft({ ...draft, markup: m, mode: "set", lines: applyMargin(lines, m), dirty: true });
  };
  const dgMissing = dgRequired && !hasDgLine(lines);

  return (
    <div className="flex flex-col gap-2">
      <div className="overflow-x-auto rounded-lg border border-[color:var(--border-subtle)]">
        <table className="w-full min-w-[1100px] border-collapse text-sm">
          <thead>
            <tr className="border-b border-[color:var(--border-subtle)] text-left text-xs text-ink-tertiary">
              <th className="w-[132px] px-2 py-1.5 font-medium">{t("oq.line.code")}</th>
              <th className="px-2 py-1.5 font-medium">{t("oq.line.description")}</th>
              <th className="w-[130px] px-2 py-1.5 font-medium">{t("oq.rates.col.basis")}</th>
              <th className="w-[72px] px-2 py-1.5 text-right font-medium">{t("oq.line.qty")}</th>
              <th className="w-[108px] px-2 py-1.5 text-right font-medium">{t("oq.line.unit_cost")}</th>
              <th className="w-[108px] px-2 py-1.5 text-right font-medium">{t("oq.line.unit_price")}</th>
              <th className="w-[110px] px-2 py-1.5 text-right font-medium">{t("field.amount")}</th>
              <th className="w-[170px] px-2 py-1.5 font-medium">{t("oq.line.vat")}</th>
              <th className="w-[64px] px-2 py-1.5 text-center font-medium"><Tip content={t("oq.line.pass_hint")}><span tabIndex={0}>{t("oq.line.pass_short")}</span></Tip></th>
              <th className="w-[40px]" />
            </tr>
          </thead>
          <tbody>
            {lines.length === 0 && (
              <tr><td colSpan={10} className="px-2 py-3 text-sm text-ink-tertiary">{t("oq.line.none")}</td></tr>
            )}
            {lines.map((l) => (
              <LineRow key={l._k} l={l} t={t} locale={locale} readOnly={readOnly} onChange={(p, kind) => update(l._k, p, kind)} onRemove={() => remove(l._k)} />
            ))}
          </tbody>
          <tfoot>
            <tr className="text-sm">
              <td colSpan={4} className="px-2 py-2 text-xs text-ink-tertiary">
                {totals.pass > 0 && t("oq.opt.pass_total", { v: eur(totals.pass, locale) })}
              </td>
              <td className="px-2 py-2 text-right tabular text-ink-secondary">{eur(totals.cost, locale)}</td>
              <td className="px-2 py-2 text-right text-xs text-ink-tertiary">{t("oq.opt.excl_vat")}</td>
              <td className="px-2 py-2 text-right tabular font-medium">{eur(totals.total, locale)}</td>
              <td colSpan={3} />
            </tr>
          </tfoot>
        </table>
      </div>
      {!readOnly && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <Button size="xs" variant="ghost" onClick={() => add()}><Plus />{t("oq.line.add")}</Button>
          <label className="inline-flex items-center gap-2 text-sm">
            <span className="text-ink-secondary">{t("oq.opt.markup")}</span>
            <NumInput className="w-20" aria-label={t("oq.opt.markup")} value={draft.markup} placeholder={draft.mode === "manual" ? t("oq.opt.manual") : ""}
              onChange={(v) => setMarkup(v)} />
            <span className="text-ink-tertiary">%</span>
          </label>
          <span className="text-xs text-ink-tertiary">{draft.mode === "manual" ? t("oq.opt.manual_hint") : t("oq.opt.markup_hint")}</span>
        </div>
      )}
      {dgMissing && (
        <div className="flex flex-wrap items-center gap-2 text-sm text-warning-fg" role="alert">
          <AlertTriangle className="size-4 shrink-0" aria-hidden />
          <span>{t("oq.opt.dg_missing")}</span>
          {!readOnly && <Button size="xs" variant="outline" onClick={() => add({ code: "DG", description: t("oq.opt.dg_line"), basis: "PER_SHIPMENT", qty: 1 })}>{t("oq.opt.add_dg")}</Button>}
        </div>
      )}
    </div>
  );
}

/**
 * One option: a compact summary row (rank, carrier, service, transit, cost / sell / margin with live values)
 * that expands to the details and the lines editor. Numbers sit in fixed-width columns so rows align.
 */
export function OptionEditor({ option, draft, onDraft, readOnly, dgRequired, accepted, rank, expanded, onToggle }) {
  const { t, locale } = useT();
  const totals = optionTotals(draft.lines);
  const tags = (option.tags || []).filter((x) => x !== option.rank_reason);
  const dgMissing = dgRequired && !hasDgLine(draft.lines);
  const bodyId = `opt-body-${option.id}`;
  return (
    <section className={cn("border-b border-[color:var(--border-subtle)] last:border-0", expanded && "bg-[var(--bg-subtle)]")} aria-label={displayText(option.carrier_name, locale) || ""}>
      <button type="button" onClick={onToggle} aria-expanded={expanded} aria-controls={bodyId}
        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-left hover:bg-hover">
        <ChevronRight className={cn("size-4 shrink-0 text-ink-tertiary transition-transform", expanded && "rotate-90")} aria-hidden />
        <span className="w-5 shrink-0 text-xs tabular text-ink-tertiary">{rank}</span>
        <span className="flex min-w-0 flex-1 basis-[220px] flex-col">
          <span className="flex min-w-0 flex-wrap items-center gap-x-2">
            <span className="truncate text-sm font-medium">{displayText(option.carrier_name, locale) || t("oq.opt.no_carrier")}</span>
            {option.recommended && <span className="inline-flex items-center gap-0.5 text-xs font-medium"><Star className="size-3" aria-hidden />{t("oq.opt.recommended")}</span>}
            {accepted && <span className="text-xs font-medium text-success-fg">{t("oq.opt.accepted")}</span>}
            {draft.dirty && <span className="text-xs text-ink-accent">{t("oq.opt.edited")}</span>}
            {dgMissing && <span className="inline-flex items-center gap-0.5 text-xs text-warning-fg"><AlertTriangle className="size-3" aria-hidden />DG</span>}
          </span>
          <span className="flex min-w-0 items-center gap-1.5 text-xs text-ink-tertiary">
            <ModeIcon mode={option.mode} className="size-3.5" />
            <span className="truncate">{[option.service ? t(`oq.service.${option.service}`) : null, option.service_name].filter(Boolean).join(" · ")}</span>
          </span>
        </span>
        <span className="hidden w-28 shrink-0 text-xs text-ink-tertiary lg:block">
          {[option.rank_reason, ...tags].filter(Boolean).slice(0, 2).map((x) => t(`oq.tag.${x}`)).join(" · ")}
        </span>
        <span className="w-20 shrink-0 text-right text-sm tabular">{isBlank(option.transit_days) ? "—" : t("oq.days", { n: option.door_to_door_days ?? option.transit_days })}</span>
        <span className="hidden w-[92px] shrink-0 sm:block"><SourcePill source={option.rate_source} /></span>
        <span className="hidden w-24 shrink-0 text-right text-sm tabular text-ink-secondary sm:block" title={t("oq.opt.cost")}>{eurTotal(totals.cost, locale)}</span>
        <span className="w-24 shrink-0 text-right text-sm font-medium tabular" title={t("oq.opt.sell")}>{eurTotal(totals.total, locale)}</span>
        <span className={cn("w-28 shrink-0 text-right text-sm tabular", totals.margin < 0 && "text-danger-fg")} title={t("oq.q.margin")}>
          {totals.marginPct === null ? "—" : `${num(totals.marginPct, locale, 1)} %`}
          <span className="block text-xs text-ink-tertiary">{eurTotal(totals.margin, locale)}</span>
        </span>
      </button>
      {expanded && (
        <div id={bodyId} className="flex flex-col gap-2 px-3 pb-3 pt-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-ink-tertiary">
            {option.carrier_id && <Link to={`/ops/carriers/${option.carrier_id}`} className="text-ink-secondary hover:underline">{t("oq.opt.carrier_link")}</Link>}
            {!isBlank(option.door_to_door_days) && <span>{t("oq.opt.d2d", { n: option.door_to_door_days })}</span>}
            {(option.etd || option.eta) && <span className="tabular">ETD {dateNum(option.etd, locale) || "—"} → ETA {dateNum(option.eta, locale) || "—"}</span>}
            {option.via?.length > 0 && <span>{t("oq.via")} {option.via.join(", ")}</span>}
            {!isBlank(option.co2e_kg) && <span>{num(option.co2e_kg, locale)} kg CO₂e</span>}
            {option.chargeable?.label && <span>{option.chargeable.label}</span>}
            {(option.liability?.label || option.liability?.label_fi) && <span>{locale === "fi" && option.liability.label_fi ? option.liability.label_fi : option.liability.label}</span>}
            {!isBlank(option.on_time_pct) && <span>{t("oq.carrier.on_time")} {num(option.on_time_pct, locale)} %</span>}
          </div>
          <LinesEditor draft={draft} onDraft={onDraft} readOnly={readOnly} dgRequired={dgRequired} />
          {option.not_included?.length > 0 && (
            <div className="text-xs text-ink-tertiary">
              <span className="font-medium text-ink-secondary">{t("oq.opt.not_included")}: </span>
              {option.not_included.map((x, i) => (
                <span key={`${x.code}-${i}`}>{i > 0 && " · "}{locale === "fi" && x.description_fi ? x.description_fi : x.description}{!isBlank(x.amount_cents) && ` ${eur(x.amount_cents, locale)}`}{(locale === "fi" && x.reason_fi ? x.reason_fi : x.reason) && ` (${locale === "fi" && x.reason_fi ? x.reason_fi : x.reason})`}</span>
              ))}
            </div>
          )}
          <Warnings items={option.warnings} />
        </div>
      )}
    </section>
  );
}
