/**
 * QuoteOptions — ranked quote options as a dense comparison list (Forto/Freightos style).
 *
 * Props (stable):
 *   options         Option[] in rank order (api-contract "Option")
 *   onSelect        (option) => void      the select / CTA button; omit to hide the button
 *   selectLabel     string                button text (e.g. "Book this")
 *   selectedId      option id shown as selected (button reads "Selected", row outlined)
 *   disabledReason  string | null         when set, select buttons are disabled and the reason is shown above the list
 *   compact         bool                  always use the stacked layout (narrow containers); otherwise rows stack below 1024 px
 *   busyId          option id whose button shows a spinner (request in flight)
 *   incoterm        string  shown with every total (e.g. "FCA")
 *   incotermPlace   string  optional
 *   validUntil      ISO     fallback validity when an option has none
 *   pricedAt        ISO     data date for indicative rates ("rates as of …")
 *   defaultOpenId   option id whose breakdown starts open
 *   emptyText       string  text when there are no options
 * Keyboard: ArrowDown / ArrowUp move between option rows; Enter on a row toggles its breakdown.
 */
import React, { useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Loader2, Lock, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useT } from "@/lib/i18n";
import { dateShort, kg, pct } from "@/lib/format";
import { cn } from "@/lib/utils";
import { eur, isIndicative, legsLabel, modeIcon, otherPartyInfo } from "./quoteFormat";
import { displayText } from "@/lib/localizedData";
import PriceBreakdown from "./PriceBreakdown";

const TAG_ORDER = ["best_value", "cheapest", "fastest", "lowest_co2"];
// carrier | route + dates | transit · CO2e · on time | total | actions (fits next to a 20rem side card at 1440 px)
const GRID = "lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(9rem,0.8fr)_6.5rem_8rem_9rem] lg:items-center lg:gap-x-4";

function Tag({ children }) {
  return (
    <span className="inline-flex h-5 items-center whitespace-nowrap rounded-full px-1.5 text-[11px] font-medium text-ink-secondary shadow-[inset_0_0_0_0.5px_var(--border-strong)]">
      {children}
    </span>
  );
}

export function SourcePill({ source }) {
  const { t } = useT();
  if (!source) return null;
  return (
    <span className={cn("inline-flex h-5 items-center whitespace-nowrap rounded-full px-1.5 text-[11px] text-ink-secondary",
      source === "indicative" ? "border border-dashed border-[color:var(--border-strong)]" : "bg-secondary")}>
      {t(`quotes.source.${source}`)}
    </span>
  );
}

function tagsOf(o) {
  const set = new Set(Array.isArray(o.tags) ? o.tags : []);
  if (o.recommended && o.rank_reason) set.add(o.rank_reason);
  return TAG_ORDER.filter((x) => set.has(x));
}

export default function QuoteOptions({
  options, onSelect, selectLabel, selectedId, disabledReason, compact, busyId, incoterm, incotermPlace, validUntil, pricedAt,
  defaultOpenId, emptyText,
}) {
  const { t, locale } = useT();
  const [open, setOpen] = useState(() => new Set(defaultOpenId ? [defaultOpenId] : []));
  const listRef = useRef(null);
  const list = useMemo(() => (Array.isArray(options) ? options : []), [options]);
  const best = useMemo(() => {
    const min = (f) => {
      const v = list.map(f).filter((x) => x !== null && x !== undefined);
      return v.length > 1 ? Math.min(...v) : null;
    };
    return { total: min((o) => o.total_cents), transit: min((o) => o.transit_days), co2: min((o) => o.co2e_kg) };
  }, [list]);
  const anyIndicative = list.some((o) => o.rate_source === "indicative");

  const toggle = (id) => setOpen((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id);
    else n.add(id);
    return n;
  });

  const onKeyDown = (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const row = e.target.closest?.("[data-option-row]");
    if (!row || !listRef.current) return;
    const rows = Array.from(listRef.current.querySelectorAll("[data-option-row]"));
    const i = rows.indexOf(row);
    const next = rows[e.key === "ArrowDown" ? i + 1 : i - 1];
    if (next) {
      e.preventDefault();
      next.focus();
    }
  };

  if (list.length === 0) {
    return <p className="rounded-lg border border-dashed px-4 py-6 text-sm text-ink-tertiary">{emptyText || t("quotes.options.none")}</p>;
  }

  const stacked = compact;
  return (
    <div className="flex flex-col gap-2">
      {disabledReason && onSelect && (
        <p className="flex items-start gap-2 text-sm text-ink-secondary">
          <Lock className="mt-0.5 size-4 shrink-0 text-ink-tertiary" aria-hidden />
          <span>{disabledReason}</span>
        </p>
      )}
      {!stacked && (
        <div className={cn("hidden px-3 pb-1 text-xs text-ink-tertiary", GRID)} aria-hidden>
          <span>{t("quotes.opt.carrier")}</span>
          <span>{t("quotes.opt.route_dates")}</span>
          <span className="text-right">{t("quotes.opt.transit_co2")}</span>
          <span className="text-right">{t("quotes.opt.total")}</span>
          <span />
        </div>
      )}
      <ul ref={listRef} className="flex flex-col gap-2" onKeyDown={onKeyDown} aria-label={t("quotes.options.title")}>
        {list.map((o, idx) => {
          const Icon = modeIcon(o.mode);
          const isOpen = open.has(o.id);
          const selected = selectedId && selectedId === o.id;
          const tags = list.length > 1 ? tagsOf(o) : []; // ranking labels mean nothing with a single option
          const warnCount = (o.warnings || []).filter((w) => w.level === "warning" || w.level === "error").length;
          const route = legsLabel(o.legs) || (o.via?.length ? o.via.join(" → ") : "");
          const panelId = `opt-bd-${o.id}`;
          const share = otherPartyInfo(o);
          const totalBlock = (withPill) => (
            <div className="text-right">
              <div className={cn("text-md tabular whitespace-nowrap", best.total !== null && o.total_cents === best.total && "font-medium")}>
                {eur(o.total_cents, locale)}
              </div>
              <div className="text-[11px] text-ink-tertiary whitespace-nowrap">
                {t("quotes.opt.excl_vat")}{incoterm ? ` · ${incoterm}` : ""}
              </div>
              {share.partial && (
                <div className="text-[11px] text-warning-fg whitespace-nowrap">
                  {t("quotes.opt.your_share")}
                  {share.otherCents ? <span className="block text-ink-tertiary">{t("quotes.opt.other_pays", { amount: eur(share.otherCents, locale) })}</span> : null}
                </div>
              )}
              {withPill && <div className="mt-0.5"><SourcePill source={o.rate_source} /></div>}
            </div>
          );
          const button = onSelect && (
            <Button
              size="sm"
              variant={selected ? "outline" : idx === 0 ? "default" : "secondary"}
              disabled={o.bookable === false || !!disabledReason || (busyId && busyId !== o.id) || busyId === o.id}
              onClick={() => onSelect(o)}
              aria-label={`${o.bookable === false ? t("gq.not_bookable") : selected ? t("quotes.opt.selected") : selectLabel || t("quotes.opt.select")} – ${o.carrier_name || ""} ${eur(o.total_cents, locale)}`}
              data-select
            >
              {busyId === o.id ? <Loader2 className="animate-spin" /> : selected ? <Check /> : null}
              {o.bookable === false ? t("gq.not_bookable") : selected ? t("quotes.opt.selected") : selectLabel || t("quotes.opt.select")}
            </Button>
          );
          const disclosure = (
            <Button variant="ghost" size="sm" aria-expanded={isOpen} aria-controls={panelId} onClick={() => toggle(o.id)}
              aria-label={`${t("quotes.opt.breakdown")} – ${o.carrier_name || ""}`}>
              <ChevronDown className={cn("transition-transform", isOpen && "rotate-180")} />
              <span className={cn(!stacked && "lg:sr-only")}>{t("quotes.opt.breakdown")}</span>
            </Button>
          );
          return (
            <li
              key={o.id}
              data-option-row
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" && e.target === e.currentTarget) {
                  e.preventDefault();
                  toggle(o.id);
                }
              }}
              className={cn(
                "rounded-xl bg-surface outline-none shadow-[inset_0_0_0_0.5px_var(--border-default)] focus-visible:shadow-[inset_0_0_0_1.5px_var(--border-strong)]",
                selected && "shadow-[inset_0_0_0_1.5px_var(--border-strong)]"
              )}
            >
              <div className={cn("flex flex-col gap-2 px-3 py-2.5", !stacked && GRID, !stacked && "lg:gap-y-0")}>
                {/* carrier + service + tags */}
                <div className="flex min-w-0 items-start justify-between gap-3">
                  <div className="flex min-w-0 items-start gap-2">
                    <Icon className="mt-0.5 size-4 shrink-0 text-ink-secondary" aria-label={t(`enum.mode.${o.mode}`)} />
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="truncate text-sm font-medium">{displayText(o.carrier_name, locale) || t("gq.carrier_pending")}</span>
                        {warnCount > 0 && (
                          <span className="inline-flex items-center gap-0.5 text-[11px] text-warning-fg" title={t("quotes.opt.warnings", { n: warnCount })}>
                            <TriangleAlert className="size-3" aria-hidden />
                            <span className="sr-only">{t("quotes.opt.warnings", { n: warnCount })}</span>
                            {warnCount}
                          </span>
                        )}
                      </div>
                      <div className="truncate text-xs text-ink-tertiary" title={o.service_name}>{o.service_name || o.service || "—"}</div>
                      {o.firmness && <div className="mt-1"><Tag>{t(o.firmness === "firm" && isIndicative(o) ? "gq.indicative" : `gq.${o.firmness}`)}</Tag></div>}
                      {tags.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {tags.map((tg) => <Tag key={tg}>{t(`quotes.tag.${tg}`)}</Tag>)}
                        </div>
                      )}
                    </div>
                  </div>
                  <div className={cn(!stacked && "lg:hidden")}>{totalBlock(false)}</div>
                </div>
                {/* route + dates */}
                <div className="flex min-w-0 flex-col gap-0.5">
                  <div className="truncate font-mono text-xs text-ink-secondary" title={route}>{route || "—"}</div>
                  <div className="text-xs text-ink-secondary tabular">
                    <span>{dateShort(o.etd, locale)} → {dateShort(o.eta, locale)}</span>
                    <span className={cn("text-ink-tertiary", !stacked && "lg:hidden")}>
                      {" · "}{o.transit_days !== null && o.transit_days !== undefined ? t("quotes.days", { n: o.transit_days }) : "—"}
                      {" · "}{o.co2e_kg !== null && o.co2e_kg !== undefined ? `${kg(o.co2e_kg, locale)} CO₂e` : "CO₂e —"}
                      {o.on_time_pct !== null && o.on_time_pct !== undefined ? ` · ${t("quotes.opt.on_time_short", { pct: pct(o.on_time_pct, locale, 0) })}` : ""}
                    </span>
                  </div>
                </div>
                {!stacked && (
                  <>
                    <div className="hidden text-right text-xs tabular text-ink-secondary lg:block">
                      <div className={cn("text-sm text-foreground", best.transit !== null && o.transit_days === best.transit && "font-medium")}>
                        {o.transit_days !== null && o.transit_days !== undefined ? t("quotes.days", { n: o.transit_days }) : "—"}
                      </div>
                      <div className={cn(best.co2 !== null && o.co2e_kg === best.co2 && "font-medium text-foreground")}>
                        {o.co2e_kg !== null && o.co2e_kg !== undefined ? `${kg(o.co2e_kg, locale)} CO₂e` : "CO₂e —"}
                      </div>
                      <div className="text-ink-tertiary">
                        {o.on_time_pct !== null && o.on_time_pct !== undefined ? t("quotes.opt.on_time_short", { pct: pct(o.on_time_pct, locale, 0) }) : "—"}
                      </div>
                    </div>
                    <div className="hidden lg:block">{totalBlock(true)}</div>
                  </>
                )}
                {/* actions */}
                <div className={cn("flex flex-wrap items-center justify-between gap-2", !stacked && "lg:justify-end")}>
                  <div className="flex items-center gap-1.5">
                    {disclosure}
                    <span className={cn(!stacked && "lg:hidden")}><SourcePill source={o.rate_source} /></span>
                  </div>
                  {button}
                </div>
              </div>
              {isOpen && (
                <div id={panelId} className="border-t border-[color:var(--border-subtle)] px-3 py-3">
                  <PriceBreakdown option={o} incoterm={incoterm} incotermPlace={incotermPlace} validUntil={validUntil} pricedAt={pricedAt} />
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {anyIndicative && (
        <p className="text-xs text-ink-tertiary">
          {t("quotes.options.scope", { date: pricedAt ? dateShort(pricedAt, locale) : "—" })}
        </p>
      )}
    </div>
  );
}
