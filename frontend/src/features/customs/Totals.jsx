// Duty & VAT consequence of a declaration: customs value, duty, VAT base, VAT, VAT route, payable to Customs. Always an estimate
// until the broker confirms codes and rates in TARIC.
import { RecordedDecision } from "./CustomsDecision";
import React from "react";
import { money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

function Line({ label, value, strong, hint, className }) {
  return (
    <div className={cn("flex items-baseline justify-between gap-3 py-1", className)}>
      <dt className={cn("text-sm", strong ? "font-medium" : "text-ink-secondary")}>
        {label}
        {hint && <span className="ml-1 text-xs text-ink-tertiary">{hint}</span>}
      </dt>
      <dd className={cn("tabular text-sm text-right", strong && "font-medium")}>{value}</dd>
    </div>
  );
}

export default function Totals({ decl, compact = false, className }) {
  const { t, locale } = useT();
  const tot = decl?.totals || {};
  const items = decl?.items || [];
  const vatRates = [...new Set(items.map((i) => i.vat_rate_pct).filter((v) => v !== null && v !== undefined))];
  const route = decl?.vat_route;
  const estimate = tot.estimate !== false;
  const dutyComplete = tot.duty_complete !== false && items.every(i => i.duty_cents != null);
  const amount = (value, complete = true) => value == null || !complete
    ? t("customs.totals.not_calculated") : money(value, locale, { decimals: 2 });
  const vatComplete = dutyComplete && tot.vat_complete !== false && items.every(i => i.vat_cents != null);
  return (
    <div className={className}>
      <dl className="divide-y divide-[color:var(--border-subtle)]">
        {!compact && <Line label={t("customs.totals.invoice_value")} value={money(tot.invoice_value_cents, locale, { decimals: 2 })} />}
        <Line label={t("customs.totals.customs_value")} value={money(tot.customs_value_cents, locale, { decimals: 2 })} />
        <Line label={t("customs.totals.duty")} value={amount(tot.duty_cents, dutyComplete)} hint={estimate ? t("customs.estimate").toLowerCase() : null} />
        {!compact && <Line label={t("customs.totals.vat_base")} value={amount(tot.vat_base_cents, vatComplete)} />}
        <Line label={t("customs.totals.vat")} value={amount(tot.vat_cents, vatComplete)}
          hint={vatRates.length ? vatRates.map((r) => `${String(r).replace(".", locale === "fi" ? "," : ".")} %`).join(" / ") : null} />
        <Line strong label={t("customs.totals.payable")} value={amount(tot.payable_to_customs_cents, dutyComplete && vatComplete)} />
      </dl>
      <RecordedDecision decision={decl?.customs_decision} />
      {tot.freight_pending && <p className="mt-2 text-xs text-warning-fg">{t("customs.totals.freight_pending")}</p>}
      {route && (
        <p className="mt-2 text-xs text-ink-secondary">
          <span className="font-mono text-[11px] text-ink-tertiary">V-3</span> {t(`customs.totals.route.${route}`)}
        </p>
      )}
      {(tot.trade_defence_items || []).length > 0 && (
        <p className="mt-1 text-xs text-warning-fg">{t("customs.totals.trade_defence", { items: tot.trade_defence_items.join(", ") })}</p>
      )}
      {tot.duty_complete === false && <p className="mt-1 text-xs text-warning-fg">{t("customs.totals.incomplete")}</p>}
      {estimate && <p className="mt-1 text-xs text-ink-tertiary">{t("customs.totals.estimate_note")}</p>}
    </div>
  );
}
