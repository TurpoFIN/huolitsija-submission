/**
 * CarrierSelectionPanel — ranked carriers for one shipment leg, with "Assign". Embedded by V1's ops shipment detail.
 *
 * Props
 *   shipmentId  string   shipment id (required)
 *   leg         object   the leg being assigned: { seq, mode, from, to, carrier_id, carrier }
 *                        (from/to are Location objects or LOCODE strings; carrier_id/carrier mark the current carrier)
 *   onAssigned  fn       called with the PATCH /api/shipments/{shipmentId}/legs/{seq} response after a successful assign
 *
 * Data: GET /api/carriers/rank?shipment_id=&leg_seq= -> { leg, cargo_summary, items: [...], not_ranked: [...], warnings: [...] }
 *       (transit_days is door to door for the leg; etd/eta shown under it).
 * Assign: PATCH /api/shipments/{shipmentId}/legs/{seq} { carrier_id, carrier: carrier_name } (needs shipments.write).
 * Carriers that do not accept the cargo's dangerous goods are listed but cannot be assigned (reason shown).
 */
import React, { useState } from "react";
import { Check, Star, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { apiPatch, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { num } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Dash, IntegrationBadge, ModeIcon, OnTime, SourcePill, Tip, eur, isBlank, locCode } from "@/features/quotes/ops/common";
import { Warnings } from "@/features/quotes/ops/OptionEditor";
import { dateNum } from "@/features/quotes/ops/RatesTable";

export default function CarrierSelectionPanel({ shipmentId, leg, onAssigned }) {
  const { t, locale } = useT();
  const { can } = useAuth();
  const seq = leg?.seq;
  const path = shipmentId && seq !== undefined && seq !== null ? `/carriers/rank?shipment_id=${encodeURIComponent(shipmentId)}&leg_seq=${seq}` : null;
  const q = useApi(path, { enabled: !!path });
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [assigned, setAssigned] = useState(null); // carrier_id assigned in this session
  const canAssign = can("shipments.write");
  const isCurrentRow = (r) => (assigned ? r.carrier_id === assigned : !!r.current || (!!leg?.carrier_id && r.carrier_id === leg.carrier_id));

  const assign = async (row) => {
    setBusy(row.carrier_id); setError(null);
    try {
      const r = await apiPatch(`/shipments/${shipmentId}/legs/${seq}`, { carrier_id: row.carrier_id, carrier: row.carrier_name });
      setAssigned(row.carrier_id);
      onAssigned?.(r);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  if (!path) return <p className="text-xs text-ink-tertiary">{t("oq.csp.no_leg")}</p>;
  if (q.loading && !q.data) return <LoadingRows rows={3} />;
  if (q.error && !q.data) return <ErrorState compact error={q.error} onRetry={q.reload} />;
  const d = q.data || {};
  const items = d.items || [];
  const lg = d.leg || leg || {};
  // Whether the cargo is dangerous goods (when the backend says so); "accepted" is only meaningful for DG cargo.
  const cargoDg = d.dangerous_goods ?? d.cargo?.dangerous_goods ?? (d.dg ? true : undefined);
  const currentName = assigned ? items.find((r) => r.carrier_id === assigned)?.carrier_name : leg?.carrier || lg.carrier || items.find((r) => r.current)?.carrier_name;

  return (
    <div className="flex flex-col gap-2" aria-label={t("oq.csp.title")}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-tertiary">
        <span className="inline-flex items-center gap-1.5 text-sm text-foreground">
          <ModeIcon mode={lg.mode} />
          <span className="font-mono text-[13px]">{locCode(lg.from) || (typeof lg.from === "string" ? lg.from : "—")} → {locCode(lg.to) || (typeof lg.to === "string" ? lg.to : "—")}</span>
        </span>
        {d.cargo_summary && <span>{d.cargo_summary}</span>}
        <span>{t("oq.csp.current")}: <span className="text-ink-secondary">{currentName || t("oq.csp.none")}</span></span>
        {q.refreshing && <span aria-live="polite">{t("oq.updating")}</span>}
      </div>
      {q.error && q.data && <ErrorState compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
      <Warnings items={d.warnings} />
      {error && <ErrorState compact error={error} />}
      {!canAssign && <p className="text-xs text-ink-tertiary">{t("oq.csp.no_perm")}</p>}

      {items.length === 0 ? (
        <p className="rounded-lg border border-dashed px-3 py-3 text-sm text-ink-tertiary">{t("oq.csp.empty")}</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-[color:var(--border-subtle)]">
          <table className="w-full min-w-[920px] border-collapse text-sm">
            <thead>
              <tr className="border-b border-[color:var(--border-subtle)] text-left text-xs text-ink-tertiary">
                <th className="px-2 py-1.5 font-medium">{t("field.carrier")}</th>
                <th className="px-2 py-1.5 font-medium">{t("oq.rates.col.service")}</th>
                <th className="px-2 py-1.5 text-right font-medium">{t("oq.csp.price")}</th>
                <th className="px-2 py-1.5 text-right font-medium">{t("oq.opt.cost")}</th>
                <th className="px-2 py-1.5 text-right font-medium">{t("oq.rates.col.transit")}</th>
                <th className="px-2 py-1.5 text-right font-medium">{t("oq.carrier.on_time")}</th>
                <th className="px-2 py-1.5 text-right font-medium">CO₂e</th>
                <th className="px-2 py-1.5 font-medium">{t("oq.csp.dg")}</th>
                <th className="px-2 py-1.5 font-medium">{t("oq.carrier.integration")}</th>
                <th className="w-[96px] px-2 py-1.5" />
              </tr>
            </thead>
            <tbody>
              {items.map((r, i) => {
                const isCurrent = isCurrentRow(r);
                const blocked = r.dg_accepted === false;
                const tags = (r.tags || []).filter((x) => x !== r.rank_reason);
                return (
                  <tr key={`${r.carrier_id}-${r.rate_card_id || i}`} className={cn("border-b border-[color:var(--border-subtle)] last:border-0", blocked && "text-ink-tertiary", isCurrent && "bg-[var(--bg-subtle)]")}>
                    <td className="px-2 py-1.5">
                      <span className="flex flex-col">
                        <span className="flex items-center gap-1.5 whitespace-nowrap">
                          <span className="w-4 text-xs tabular text-ink-tertiary">{i + 1}</span>
                          <span className={cn("font-medium", blocked && "font-normal")}>{r.carrier_name}</span>
                          {r.recommended && <Tip content={r.rank_reason ? t(`oq.tag.${r.rank_reason}`) : null}><span tabIndex={0} className="inline-flex items-center gap-0.5 text-xs font-medium text-foreground"><Star className="size-3" aria-hidden />{t("oq.opt.recommended")}</span></Tip>}
                        </span>
                        {(tags.length > 0 || (!r.recommended && r.rank_reason)) && (
                          <span className="pl-[22px] text-xs text-ink-tertiary">{[...(!r.recommended && r.rank_reason ? [r.rank_reason] : []), ...tags].map((x) => t(`oq.tag.${x}`)).join(" · ")}</span>
                        )}
                      </span>
                    </td>
                    <td className="max-w-[200px] px-2 py-1.5">
                      <span className="block truncate" title={r.service_name || ""}>{r.service_name || <Dash />}</span>
                      <span className="flex items-center gap-1.5 text-xs text-ink-tertiary">
                        {r.service && <span>{t(`oq.service.${r.service}`)}</span>}
                        {r.mode && r.mode !== lg.mode && <span>{tEnum(t, "mode", r.mode)}</span>}
                        {r.rate_source && <SourcePill source={r.rate_source} />}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-right tabular whitespace-nowrap">{isBlank(r.price_cents) ? <Dash /> : eur(r.price_cents, locale)}</td>
                    <td className="px-2 py-1.5 text-right tabular whitespace-nowrap text-ink-secondary">{isBlank(r.cost_cents) ? <Dash /> : eur(r.cost_cents, locale)}</td>
                    <td className="px-2 py-1.5 text-right tabular whitespace-nowrap">
                      {isBlank(r.transit_days) ? <Dash /> : t("oq.days", { n: num(r.transit_days, locale) })}
                      {(r.etd || r.eta) && <span className="block text-xs text-ink-tertiary">{dateNum(r.etd, locale) || "—"} → {dateNum(r.eta, locale) || "—"}</span>}
                    </td>
                    <td className="px-2 py-1.5 text-right"><OnTime value={r.on_time_pct} source={r.on_time_source} /></td>
                    <td className="px-2 py-1.5 text-right tabular whitespace-nowrap">{isBlank(r.co2e_kg) ? <Dash /> : `${num(r.co2e_kg, locale)} kg`}</td>
                    <td className="px-2 py-1.5">
                      {blocked ? (
                        <span className="inline-flex items-start gap-1 text-xs text-danger-fg"><XCircle className="mt-px size-3.5 shrink-0" aria-hidden /><span>{r.dg_reason || t("oq.csp.dg_no")}</span></span>
                      ) : r.dg_accepted === true && cargoDg !== false ? (
                        <span className="inline-flex items-center gap-1 text-xs text-ink-secondary"><Check className="size-3.5" aria-hidden />{t("oq.csp.dg_ok")}</span>
                      ) : <span className="text-xs text-ink-tertiary">{t("oq.csp.dg_na")}</span>}
                    </td>
                    <td className="px-2 py-1.5"><IntegrationBadge status={r.integration_status} /></td>
                    <td className="px-2 py-1.5 text-right">
                      {isCurrent ? (
                        <span className="inline-flex items-center gap-1 text-xs font-medium"><Check className="size-3.5" aria-hidden />{t("oq.csp.assigned")}</span>
                      ) : (
                        <Tip content={blocked ? (r.dg_reason || t("oq.csp.dg_no")) : !canAssign ? t("oq.csp.no_perm") : null}>
                          <span tabIndex={blocked || !canAssign ? 0 : undefined}>
                            <Button size="xs" variant={r.recommended ? "default" : "outline"} disabled={blocked || !canAssign || !!busy} onClick={() => assign(r)}>
                              {busy === r.carrier_id ? t("state.saving") : t("oq.csp.assign")}
                            </Button>
                          </span>
                        </Tip>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {d.not_ranked?.length > 0 && (
        <p className="text-xs text-ink-tertiary">
          <span className="font-medium text-ink-secondary">{t("oq.csp.not_ranked")}: </span>
          {d.not_ranked.map((n, i) => <span key={i}>{i > 0 && " · "}{n.carrier_name} ({n.reason})</span>)}
        </p>
      )}
    </div>
  );
}
export { CarrierSelectionPanel };
