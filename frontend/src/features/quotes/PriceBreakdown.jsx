/**
 * PriceBreakdown — one option's price lines grouped by stage, totals, what is not included, liability and validity.
 *
 * Props (stable):
 *   option     Option (api-contract): lines[{code, description, stage, basis, qty, unit_price_cents, amount_cents, vat_code,
 *              vat_rate_pct, is_pass_through, estimated, note}], not_included[], total_cents, vat_cents, total_incl_vat_cents,
 *              liability{regime, limit_sdr_per_kg, label} | liability_regime, valid_until, rate_source, chargeable{label}, warnings[]
 *   incoterm   string, e.g. "FCA" (shown next to the total; every price carries its Incoterm)
 *   incotermPlace  string (optional)
 *   validUntil ISO (fallback when option.valid_until is missing)
 *   pricedAt   ISO date the rates were taken (shown as "rates as of" for indicative prices)
 *   showWarnings  bool (default true) render option.warnings under the totals
 *   className
 * Money is right-aligned tabular, always 2 decimals. Missing values show "—".
 */
import React from "react";
import { useT } from "@/lib/i18n";
import { date, num } from "@/lib/format";
import { cn } from "@/lib/utils";
import { STAGES, chargeableText, eur, lineText, liabilityLabel, reasonText, validityLabel, vatLabel } from "./quoteFormat";
import PriceSources from "./PriceSources";
import { displayText } from "@/lib/localizedData";
import WarningsList from "./WarningsList";

function humanize(s) {
  return String(s || "").replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
}

function unitLabel(t, basis) {
  if (!basis) return "";
  const k = `quotes.unit.${basis}`;
  const s = t(k);
  return s === k ? humanize(basis.replace(/^PER_/, "")) : s;
}

const unitless = (l) => ["PERCENT", "PCT", "ACTUAL"].includes(l.basis);

export function qtyLabel(t, line, locale) {
  if (line.basis === "PERCENT" || line.basis === "PCT") {
    const m = /^\s*(\d+(?:[.,]\d+)?)\s*%/.exec(line.note || "");
    return m ? `${num(Number(m[1].replace(",", ".")), locale, 2)} %` : "—";
  }
  if (line.basis === "ACTUAL") return t("quotes.unit.ACTUAL");
  if (line.qty === null || line.qty === undefined) return "—";
  const u = unitLabel(t, line.basis);
  return `${num(line.qty, locale, 2)}${u ? ` ${u}` : ""}`;
}

export default function PriceBreakdown({ option, incoterm, incotermPlace, validUntil, pricedAt, showWarnings = true, className }) {
  const { t, locale } = useT();
  if (!option) return null;
  const lines = Array.isArray(option.lines) ? option.lines : [];
  const order = [...STAGES];
  lines.forEach((l) => {
    const s = l.stage || (l.is_pass_through ? "pass_through" : "fees");
    if (!order.includes(s)) order.push(s);
  });
  const groups = order
    .map((s) => ({ stage: s, lines: lines.filter((l) => (l.stage || (l.is_pass_through ? "pass_through" : "fees")) === s) }))
    .filter((g) => g.lines.length);
  const stageLabel = (s) => {
    const k = `quotes.stage.${s}`;
    const v = t(k);
    return v === k ? humanize(s) : v;
  };
  const valid = option.valid_until || validUntil;
  const notIncluded = Array.isArray(option.not_included) ? option.not_included : [];
  const liability = option.liability || option.liability_regime;

  return (
    <div className={cn("flex flex-col gap-4", className)}>
      <PriceSources option={option} />
      {(option.chargeable || option.rate_source) && (
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-tertiary">
          {chargeableText(t, option.chargeable, locale) && (
            <span title={option.chargeable?.label || undefined}>
              {t("quotes.bd.chargeable")}: <span className="text-ink-secondary tabular">{chargeableText(t, option.chargeable, locale)}</span>
              {option.chargeable?.weight_kg || option.chargeable?.volume_m3 ? (
                <span className="tabular"> ({[option.chargeable.weight_kg ? `${num(option.chargeable.weight_kg, locale)} kg` : null,
                  option.chargeable.volume_m3 ? `${num(option.chargeable.volume_m3, locale, 2)} m³` : null].filter(Boolean).join(", ")} {t("quotes.bd.actual")})</span>
              ) : null}
            </span>
          )}
          {option.rate_source && (
            <span>
              {t(`quotes.source.${option.rate_source}`)}
              {pricedAt ? ` · ${t("quotes.bd.rates_as_of", { date: date(pricedAt, locale) })}` : ""}
            </span>
          )}
        </p>
      )}

      {groups.length === 0 ? (
        <p className="text-sm text-ink-tertiary">{t("quotes.bd.no_lines")}</p>
      ) : (
        <table className="w-full text-sm">
          <caption className="sr-only">{t("quotes.bd.title")}</caption>
          <thead>
            <tr className="text-xs text-ink-tertiary">
              <th scope="col" className="py-1 pr-3 text-left font-normal">{t("quotes.bd.description")}</th>
              <th scope="col" className="hidden py-1 px-3 text-right font-normal sm:table-cell">{t("quotes.bd.qty")}</th>
              <th scope="col" className="hidden py-1 px-3 text-right font-normal sm:table-cell">{t("quotes.bd.unit_price")}</th>
              <th scope="col" className="py-1 pl-3 text-right font-normal">{t("quotes.bd.amount")}</th>
              <th scope="col" className="hidden py-1 pl-3 text-right font-normal sm:table-cell">{t("quotes.bd.vat")}</th>
            </tr>
          </thead>
          {groups.map((g) => {
            const sub = g.lines.reduce((a, l) => a + (l.amount_cents || 0), 0);
            return (
              <tbody key={g.stage} className="border-t border-[color:var(--border-subtle)]">
                <tr>
                  <th scope="rowgroup" colSpan={5} className="pt-2.5 pb-1 text-left text-xs font-medium text-ink-secondary">{stageLabel(g.stage)}</th>
                </tr>
                {g.lines.map((l, i) => (
                  <tr key={`${l.code}-${i}`} className="align-top">
                    <td className="py-1 pr-3">
                      <span>{lineText(l, locale)}</span>
                      {l.estimated && <span className="ml-1.5 rounded-full px-1.5 text-[11px] text-ink-tertiary shadow-[inset_0_0_0_0.5px_var(--border-default)]">{t("quotes.bd.estimated")}</span>}
                      {(l.note || l.note_fi) && <span className="block text-xs text-ink-tertiary">{displayText((locale === "fi" && l.note_fi) || l.note, locale)}</span>}
                      <span className="block text-xs text-ink-tertiary tabular sm:hidden">
                        {unitless(l) ? qtyLabel(t, l, locale) : `${qtyLabel(t, l, locale)} × ${eur(l.unit_price_cents, locale)}`} · {vatLabel(t, l.vat_code, l.vat_rate_pct, locale)}
                      </span>
                    </td>
                    <td className="hidden py-1 px-3 text-right tabular text-ink-secondary whitespace-nowrap sm:table-cell">{qtyLabel(t, l, locale)}</td>
                    <td className="hidden py-1 px-3 text-right tabular text-ink-secondary whitespace-nowrap sm:table-cell">{unitless(l) ? "—" : eur(l.unit_price_cents, locale)}</td>
                    <td className="py-1 pl-3 text-right tabular whitespace-nowrap">{eur(l.amount_cents, locale)}</td>
                    <td className="hidden py-1 pl-3 text-right text-xs text-ink-tertiary whitespace-nowrap sm:table-cell">{vatLabel(t, l.vat_code, l.vat_rate_pct, locale)}</td>
                  </tr>
                ))}
                <tr>
                  <td className="pb-2 pt-0.5 pr-3 text-xs text-ink-tertiary">{t("quotes.bd.subtotal")}</td>
                  <td className="hidden sm:table-cell" />
                  <td className="hidden sm:table-cell" />
                  <td className="pb-2 pt-0.5 pl-3 text-right text-sm font-medium tabular whitespace-nowrap">{eur(sub, locale)}</td>
                  <td className="hidden sm:table-cell" />
                </tr>
              </tbody>
            );
          })}
        </table>
      )}

      <dl className="ml-auto flex w-full max-w-sm flex-col gap-1 border-t border-[color:var(--border-default)] pt-2 text-sm">
        <div className="flex items-baseline justify-between gap-4">
          <dt className="font-medium">{t("quotes.bd.total_excl")}{incoterm ? <span className="ml-1.5 text-xs font-normal text-ink-tertiary">{incoterm}{incotermPlace ? ` ${incotermPlace}` : ""}</span> : null}</dt>
          <dd className="text-md font-medium tabular whitespace-nowrap">{eur(option.total_cents, locale)}</dd>
        </div>
        <div className="flex items-baseline justify-between gap-4 text-ink-secondary">
          <dt>{t("quotes.bd.vat_est")}</dt>
          <dd className="tabular whitespace-nowrap">{eur(option.vat_cents, locale)}</dd>
        </div>
        <div className="flex items-baseline justify-between gap-4 text-ink-secondary">
          <dt>{t("quotes.bd.total_incl")}</dt>
          <dd className="tabular whitespace-nowrap">{eur(option.total_incl_vat_cents, locale)}</dd>
        </div>
      </dl>

      <div>
        <h4 className="mb-1 text-xs font-medium text-ink-secondary">{t("quotes.bd.not_included")}</h4>
        {notIncluded.length === 0 ? (
          <p className="text-xs text-ink-tertiary">{t("quotes.bd.not_included_none")}</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {notIncluded.map((n, i) => (
              <li key={`${n.code}-${i}`} className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 text-sm">
                <span className="min-w-0">
                  {lineText(n, locale)}
                  {reasonText(t, n, incoterm, locale) && <span className="block text-xs text-ink-tertiary">{reasonText(t, n, incoterm, locale)}</span>}
                </span>
                <span className="text-right tabular text-ink-secondary whitespace-nowrap">
                  {n.amount_cents === null || n.amount_cents === undefined ? "—" : `${t("quotes.bd.approx")} ${eur(n.amount_cents, locale)}`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <dl className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
        <div className="flex gap-1.5">
          <dt className="text-ink-tertiary">{t("quotes.bd.liability")}:</dt>
          <dd className="text-ink-secondary">{liabilityLabel(t, liability, locale)}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-ink-tertiary">{t("quotes.bd.valid_until")}:</dt>
          <dd className="text-ink-secondary tabular">{valid ? `${date(valid, locale)} (${validityLabel(t, valid)})` : "—"}</dd>
        </div>
      </dl>

      {showWarnings && <WarningsList warnings={option.warnings} dense />}
    </div>
  );
}
