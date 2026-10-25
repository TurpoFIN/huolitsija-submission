// Ops carrier detail: profile, DG acceptance, integration readiness, performance and the carrier's rate cards.
import { useConfirmChange } from "@/components/app/ConfirmChange";
import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ExternalLink, Pencil, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { KV, PageHeader, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPatch, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { DgClasses, IntegrationBadge, ModeIcons, OnTime, RateCounts, isBlank, locCode } from "@/features/quotes/ops/common";
import { CarrierDialog } from "@/features/quotes/ops/CarrierDialog";
import { RatesTable } from "@/features/quotes/ops/RatesTable";
import { RateSheet } from "@/features/quotes/ops/RateSheet";

function IntegrationBlock({ integ }) {
  const { t, locale } = useT();
  if (!integ) return <p className="text-sm text-ink-tertiary">—</p>;
  const env = integ.env || [];
  return (
    <div className="flex flex-col gap-3">
      <KV cols={2} items={[
        { label: t("oq.carrier.integration_type"), value: integ.type ? t(`oq.integration.${integ.type}`) : null },
        { label: t("field.status"), value: <IntegrationBadge status={integ.status} /> },
        { label: t("oq.carrier.docs"), value: integ.docs_url ? (
          <a href={integ.docs_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-ink-accent hover:underline">
            {integ.docs_url.replace(/^https?:\/\//, "").replace(/\/$/, "")}<ExternalLink className="size-3" aria-hidden />
          </a>
        ) : null },
        { label: t("oq.carrier.integration_note"), value: locale === "fi" && integ.note_fi ? integ.note_fi : integ.note },
      ]} />
      {integ.status !== "live" && (
        <p className="text-xs text-ink-tertiary">
          {t("review.carrier.setup")}
        </p>
      )}
    </div>
  );
}

const endText = (e) => (typeof e === "string" ? e : locCode(e) || e?.name || "—");

function RecentLegs({ legs }) {
  const { t, locale } = useT();
  if (!legs || legs.length === 0) return <p className="py-1 text-sm text-ink-tertiary">{t("oq.carrier.no_legs")}</p>;
  return (
    <ul className="divide-y divide-[color:var(--border-subtle)]">
      {legs.map((l, i) => (
        <li key={`${l.shipment_id}-${l.seq ?? i}`}>
          <Link to={l.shipment_id ? `/ops/shipments/${l.shipment_id}` : "#"} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 rounded-md px-1 py-2 hover:bg-hover">
            <span className="w-36 shrink-0 text-sm font-medium">{l.shipment_reference || l.reference || "—"}</span>
            <span className="min-w-0 flex-1 truncate text-sm text-ink-secondary">{endText(l.from)} → {endText(l.to)}</span>
            {l.status && <StatusBadge size="sm" value={l.status} />}
            <span className="w-28 text-right text-xs tabular text-ink-tertiary">{l.ata ? `${t("oq.carrier.arrived")} ${date(l.ata, locale)}` : l.eta ? `ETA ${date(l.eta, locale)}` : "—"}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

export default function CarrierDetail() {
  const { id } = useParams();
  const { t, locale } = useT();
  const confirmation = useConfirmChange();
  const { can } = useAuth();
  const canWrite = can("rates.write");
  const res = useApi(`/carriers/${id}`);
  const [editing, setEditing] = useState(false);
  const [sheet, setSheet] = useState({ open: false, rate: null });
  const [toggleErr, setToggleErr] = useState(null);
  const [toggling, setToggling] = useState(false);

  const toggleActive = async (v) => {
    setToggling(true); setToggleErr(null);
    try {
      const r = await apiPatch(`/carriers/${id}`, { active: v });
      res.setData((d) => ({ ...d, carrier: { ...d.carrier, ...(r.carrier || {}), active: (r.carrier || {}).active ?? v } }));
    } catch (e) {
      setToggleErr(e);
    } finally {
      setToggling(false);
    }
  };

  return (
    <Loadable q={res} skeleton={<div><LoadingRows rows={2} /><LoadingRows rows={8} /></div>}>
      {(d) => {
        const c = d.carrier || {};
        const perf = d.performance || c.performance || {};
        const rates = d.rates || [];
        const stats = c.stats || {
          rate_cards: rates.length,
          valid: rates.filter((r) => r.state === "valid").length,
          expiring: rates.filter((r) => r.state === "expiring").length,
          expired: rates.filter((r) => r.state === "expired").length,
        };
        return (
          <div>
            <PageHeader
              crumbs={[{ label: t("nav.carriers"), to: "/ops/carriers" }, { label: c.name }]}
              title={c.name}
              meta={
                <>
                  <span className="font-mono text-xs">{c.code}</span>
                  <ModeIcons modes={c.modes} />
                  <IntegrationBadge status={c.integration?.status} />
                  {c.active === false && <StatusBadge size="sm" state="expired" value="inactive" label={t("oq.carrier.inactive")} />}
                  <Freshness at={res.updatedAt} onRefresh={res.reload} refreshing={res.refreshing} />
                </>
              }
              actions={canWrite && (
                <>
                  <label className="inline-flex items-center gap-2 text-sm text-ink-secondary">
                    <Switch checked={c.active !== false} disabled={toggling} onCheckedChange={v=>confirmation.confirm(()=>toggleActive(v), `${t("oq.carrier.active")}: ${t(v ? "review.permission_yes" : "review.permission_no")}`)} aria-label={t("oq.carrier.active")} />
                    {c.active !== false ? t("oq.carrier.active_short") : t("oq.carrier.inactive")}
                  </label>
                  <Button variant="outline" onClick={() => setEditing(true)}><Pencil />{t("action.edit")}</Button>
                </>
              )}
            />
            {confirmation.dialog}
            {toggleErr && <ErrorState className="mb-3" compact error={toggleErr} />}

            <p className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
              <RateCounts inline stats={stats} />
              <span className="text-ink-secondary">{t("oq.carrier.dg")}: <DgClasses classes={c.dg_classes_accepted} /></span>
              <span className="text-ink-secondary">{t("oq.carrier.on_time")}: <OnTime value={perf.on_time_pct ?? c.on_time_pct} source={perf.source} sample={perf.sample_size} /></span>
            </p>

            <div className="grid gap-x-8 lg:grid-cols-2">
              <Section title={t("oq.carrier.profile")}>
                <KV cols={2} items={[
                  { label: t("oq.carrier.contact"), value: c.contact_email ? <a className="text-ink-accent hover:underline" href={`mailto:${c.contact_email}`}>{c.contact_email}</a> : null },
                  { label: t("oq.carrier.website"), value: c.website ? <a className="inline-flex items-center gap-1 text-ink-accent hover:underline" href={c.website} target="_blank" rel="noreferrer">{c.website.replace(/^https?:\/\//, "").replace(/\/$/, "")}<ExternalLink className="size-3" aria-hidden /></a> : null },
                  { label: t("oq.carrier.country"), value: c.country },
                  { label: t("oq.carrier.co2"), value: isBlank(c.co2_factor_g_tkm) ? null : `${num(c.co2_factor_g_tkm, locale, 1)} g/tkm` },
                  { label: t("oq.carrier.services_note"), value: c.services_note, className: "col-span-full" },
                ]} />
              </Section>
              <Section title={t("oq.carrier.integration")}>
                <IntegrationBlock integ={c.integration} />
              </Section>
              <Section title={t("oq.carrier.dg_title")}>
                <KV cols={2} items={[
                  { label: t("oq.carrier.dg_classes"), value: <DgClasses full classes={c.dg_classes_accepted} /> },
                  { label: t("oq.carrier.dg_blocked"), value: c.dg_un_blocked?.length ? <span className="font-mono text-[13px]">{c.dg_un_blocked.join(", ")}</span> : t("oq.dg.none_blocked") },
                  { label: t("oq.carrier.dg_note"), value: c.dg_note, className: "col-span-full" },
                ]} />
              </Section>
              <Section title={t("oq.carrier.performance")}>
                <KV cols={2} items={[
                  { label: t("oq.carrier.on_time"), value: isBlank(perf.on_time_pct ?? c.on_time_pct) ? null : `${num(perf.on_time_pct ?? c.on_time_pct, locale, 1)} %` },
                  { label: t("oq.carrier.perf_source"), value: perf.source ? t(`oq.perf_source.${perf.source}`) : null },
                  { label: t("oq.carrier.sample"), value: isBlank(perf.sample_size) ? null : t("oq.carrier.sample_legs", { n: num(perf.sample_size, locale) }) },
                  { label: t("oq.carrier.perf_note"), value: locale === "fi" && perf.note_fi ? perf.note_fi : perf.note },
                ]} />
              </Section>
            </div>

            <Section
              title={`${t("oq.carrier.rate_cards")} · ${num(rates.length, locale)}`}
              actions={canWrite && <Button size="sm" variant="outline" onClick={() => setSheet({ open: true, rate: null })}><Plus />{t("oq.rates.add")}</Button>}
            >
              <RatesTable rows={rates} showCarrier={false} onEdit={(r) => setSheet({ open: true, rate: r })}
                empty={<EmptyState title={t("oq.carrier.no_rates")} body={t("oq.carrier.no_rates_body")} action={canWrite && <Button size="sm" onClick={() => setSheet({ open: true, rate: null })}><Plus />{t("oq.rates.add")}</Button>} />} />
            </Section>

            <Section title={t("oq.carrier.recent_legs")}>
              <RecentLegs legs={d.recent_legs} />
            </Section>

            <CarrierDialog open={editing} onOpenChange={setEditing} carrier={c} onSaved={() => res.reload()} />
            <RateSheet open={sheet.open} onOpenChange={(o) => setSheet((s) => ({ ...s, open: o }))} rate={sheet.rate}
              carriers={[{ id: c.id, name: c.name, code: c.code }]} defaultCarrierId={c.id} canWrite={canWrite}
              onSaved={() => res.reload()} onArchived={() => res.reload()} />
          </div>
        );
      }}
    </Loadable>
  );
}
