// Liability cap block: regime + basis, cap in SDR and EUR at a dated SDR rate, time bar, notice deadline, notes.
import React from "react";
import { AlertTriangle } from "lucide-react";
import { StatusBadge } from "@/components/app/StatusBadge";
import { date, money, num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { daysLeft, localBasis, localNote, sdrSource } from "./text";

function Row({ label, children }) {
  return (
    <>
      <dt className="pt-0.5 text-xs text-ink-tertiary">{label}</dt>
      <dd className="min-w-0 text-sm">{children}</dd>
    </>
  );
}

export function TimeBarText({ iso, decided, className }) {
  const { t, locale } = useT();
  if (!iso) return <span className="text-ink-tertiary">—</span>;
  const d = daysLeft(iso);
  const tone = decided ? null : d < 0 ? "danger" : d <= 20 ? "warning" : null;
  return (
    <span className={cn("inline-flex flex-col leading-tight", className)}>
      <span className="tabular">{date(iso, locale)}</span>
      {!decided && (
        <span className={cn("text-xs", tone === "danger" ? "font-medium text-danger-fg" : tone === "warning" ? "font-medium text-warning-fg" : "text-ink-tertiary")}>
          {d < 0 ? t("claims.tb.passed") : d === 0 ? t("claims.tb.today") : t("claims.tb.days", { n: num(d, locale) })}
        </span>
      )}
    </span>
  );
}

/**
 * est: estimate shape (GET /claims/estimate, or capFromClaim(claim)).
 * claimedCents: amount claimed, for the over-cap warning. decided: hide countdowns once a claim is decided.
 */
export default function CapCalculation({ est, claimedCents, loading, error, decided, className, emptyText }) {
  const { t, locale } = useT();
  if (!est) {
    return (
      <div className={cn("rounded-xl bg-subtle p-3", className)}>
        <h3 className="mb-1 text-sm font-medium">{t("claims.cap.title")}</h3>
        {error ? <p className="text-sm text-danger-fg">{error.message}</p>
          : <p className="text-sm text-ink-tertiary">{loading ? t("claims.cap.loading") : emptyText || t("claims.cap.pick")}</p>}
      </div>
    );
  }
  const rate = est.sdr_rate;
  const hasEur = est.cap_eur_cents !== null && est.cap_eur_cents !== undefined;
  const hasSdr = est.cap_sdr !== null && est.cap_sdr !== undefined;
  const over = hasEur && claimedCents && claimedCents > est.cap_eur_cents;
  const tbDays = daysLeft(est.time_bar_date);
  const notes = (est.notes || []).map((n) => localNote(n, t)).filter(Boolean);

  return (
    <section aria-label={t("claims.cap.title")} className={cn("flex flex-col gap-3 rounded-xl bg-subtle p-3", className)}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-medium">{t("claims.cap.title")} · {est.regime_label || est.regime || "—"}</h3>
          <p className="text-xs text-ink-secondary">{localBasis(est.basis, t, locale) || "—"}</p>
          <p className="text-xs text-ink-tertiary">{est.regime_source === "shipment" ? t("claims.cap.source_shipment") : t("claims.cap.source_mode")}</p>
        </div>
        {loading && <span className="shrink-0 text-xs text-ink-tertiary" aria-live="polite">{t("claims.cap.loading")}</span>}
      </div>

      <dl className="grid grid-cols-[minmax(96px,auto)_1fr] gap-x-4 gap-y-2.5">
        <Row label={t("claims.cap.max")}>
          {hasEur || hasSdr ? (
            <>
              <span className="flex flex-wrap items-baseline gap-x-2">
                {hasEur && <span className="font-medium tabular">{money(est.cap_eur_cents, locale, { decimals: 2 })}</span>}
                {hasSdr && <span className="text-ink-secondary tabular">{num(est.cap_sdr, locale, 2)} SDR</span>}
              </span>
              {hasSdr && rate && (
                <span className="block text-xs text-ink-tertiary tabular">
                  {t("claims.cap.rate", { rate: num(rate.eur_per_sdr, locale, 5), source: sdrSource(rate.source, t), date: date(rate.date, locale) })}
                </span>
              )}
              {hasSdr && rate?.stale && <span className="block text-xs text-warning-fg">{t("claims.cap.stale")}</span>}
              {hasSdr && !rate && <span className="block text-xs text-warning-fg">{t("claims.cap.no_rate")}</span>}
            </>
          ) : (
            <span className="text-ink-tertiary">{t("claims.cap.unknown")}</span>
          )}
        </Row>
        <Row label={t("claims.cap.time_bar")}>
          {est.time_bar_date ? (
            <>
              <span className="tabular">{date(est.time_bar_date, locale)}</span>
              {!decided && tbDays !== null && (
                <span className={cn("ml-2 text-xs", tbDays < 0 ? "font-medium text-danger-fg" : tbDays <= 20 ? "font-medium text-warning-fg" : "text-ink-tertiary")}>
                  {tbDays < 0 ? t("claims.tb.passed") : tbDays === 0 ? t("claims.tb.today") : t("claims.tb.days", { n: num(tbDays, locale) })}
                </span>
              )}
              <span className="block text-xs text-ink-tertiary">
                {t("claims.cap.time_bar_src")}
                {est.delivery_date ? ` · ${t("claims.cap.delivered")} ${date(est.delivery_date, locale)}` : ""}
              </span>
              {est.regime_time_bar_date && (
                <span className="block text-xs text-ink-tertiary">{t("claims.cap.regime_time_bar", { regime: est.regime_label, date: date(est.regime_time_bar_date, locale) })}</span>
              )}
            </>
          ) : (
            <span className="text-ink-tertiary">{t("claims.cap.no_delivery")}</span>
          )}
        </Row>
        <Row label={t("claims.cap.notice")}>
          {est.notice_deadline ? (
            <>
              <span className="inline-flex flex-wrap items-center gap-2">
                <span className="tabular">{date(est.notice_deadline, locale)}</span>
                {est.notice_late && <StatusBadge size="sm" state="pending" label={t("claims.cap.notice_late")} />}
              </span>
              <span className="block text-xs text-ink-tertiary">{est.notice_source}</span>
            </>
          ) : (
            <span className="text-ink-tertiary">{t("claims.cap.notice_none")}{est.notice_source ? ` · ${est.notice_source}` : ""}</span>
          )}
        </Row>
        <Row label={t("claims.cap.role")}>{t(`claims.cap.role.${est.forwarder_role === "intermediary" ? "intermediary" : "principal"}`)}</Row>
      </dl>

      {est.notice_late && <p className="text-xs text-warning-fg">{t("claims.cap.notice_late_body")}</p>}
      {notes.length > 0 && (
        <ul className="flex flex-col gap-1 text-xs text-ink-tertiary">
          {notes.map((n, i) => <li key={i}>{n}</li>)}
        </ul>
      )}
      {over && (
        <p role="alert" className="flex items-start gap-2 rounded-lg px-2.5 py-2 text-sm status-pending">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>{t("claims.cap.over", { claimed: money(claimedCents, locale, { decimals: 2 }), cap: money(est.cap_eur_cents, locale, { decimals: 2 }) })}</span>
        </p>
      )}
      {error && <p className="text-xs text-danger-fg">{error.message}</p>}
    </section>
  );
}
