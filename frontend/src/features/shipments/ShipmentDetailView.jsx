// Shipment detail for the customer portal and ops console.
// Decision first: status, route, delivery ETA vs plan, next milestone; then exceptions, milestones, legs, map, facts.
import Forecast from "@/features/visibility/Forecast";
import React, { Suspense, lazy } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { AlertTriangle, ExternalLink, MessageSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { apiDelete, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import ShipmentMap from "@/features/map/ShipmentMap";
import {
  DELIVERY_BLOCKING, DataAge, DemoTag, ModeLabel, PanelBoundary, RouteText, date, dateTime, holdLabel, msLabel, relTime, sourceLabel,
} from "@/features/shipments/common";
import { EventHistory, MilestoneTimeline } from "@/features/shipments/Timeline";
import { LegsTable } from "@/features/shipments/Legs";
import { CargoFacts, CompanyChecks, PartiesFacts, ReferencesFacts, TermsFacts, TrackingFeeds } from "@/features/shipments/Facts";
import { InternalNotes, OpsActionBar } from "@/features/shipments/OpsActions";

const TradeDocumentsPanel = lazy(() => import("@/features/tradedocs/TradeDocumentsPanel"));
const DocumentsPanel = lazy(() => import("@/features/documents/ShipmentDocumentsPanel"));
const CustomsPanel = lazy(() => import("@/features/customs/ShipmentCustomsPanel"));
const InvoicesPanel = lazy(() => import("@/features/billing/ShipmentInvoicesPanel"));
const MessagesPanel = lazy(() => import("@/features/messages/ShipmentMessagesPanel"));
const ExceptionsPanel = lazy(() => import("@/features/exceptions/ShipmentExceptionsPanel"));

const EUROPE = new Set(("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE NO IS LI CH GB UK " +
  "AL BA ME MK RS MD UA TR AD MC SM VA FO GI").split(" "));

export function mapView(s) {
  const o = s.origin?.country;
  const d = s.destination?.country;
  if (!o || !d) return "world";
  return EUROPE.has(o) && EUROPE.has(d) ? "europe" : "world";
}

const TABS = ["overview", "documents", "customs", "invoices", "messages", "exceptions", "history"];

function Panel({ children }) {
  const { t } = useT();
  return (
    <PanelBoundary message={t("shipments.panel_unavailable")}>
      <Suspense fallback={<LoadingRows rows={4} />}>{children}</Suspense>
    </PanelBoundary>
  );
}

export default function ShipmentDetailView({ scope = "customer" }) {
  const ops = scope === "ops";
  const { id } = useParams();
  const { t } = useT();
  const [params, setParams] = useSearchParams();
  const tab = TABS.includes(params.get("tab")) ? params.get("tab") : "overview";
  const q = useApi(`/shipments/${id}`, { pollMs: 30000 });
  const base = ops ? "/ops/shipments" : "/portal/shipments";
  const s = q.data;

  if (q.loading && !s) return <div><div className="mb-4 h-14 animate-pulse rounded-lg bg-[var(--bg-subtle)]" /><LoadingRows rows={10} /></div>;
  if (q.error && !s) {
    return (
      <div>
        <PageHeader title={t("shipments.title")} crumbs={[{ label: t("shipments.detail.back"), to: base }]} />
        <ErrorState error={q.error.status === 404 ? { message: t("shipments.detail.not_found") } : q.error} onRetry={q.error.status === 404 ? undefined : q.reload} />
      </div>
    );
  }
  if (!s) return null;

  const setTab = (v) => {
    const p = new URLSearchParams(params);
    if (v === "overview") p.delete("tab");
    else p.set("tab", v);
    setParams(p, { replace: true });
  };
  const reload = () => q.reload();
  const tabLabels = { overview: t("shipments.tab.overview"), documents: t("shipments.tab.documents"), customs: t("shipments.tab.customs"),
    invoices: t("shipments.tab.invoices"), messages: t("shipments.tab.messages"), exceptions: t("shipments.tab.exceptions"), history: t("shipments.tab.history") };
  // V3 panels (documents, customs) take mode "portal" | "ops"; V4 panels (invoices, messages, exceptions) take "customer" | "ops".
  const v3Props = { shipment: s, mode: ops ? "ops" : "portal", onChange: reload };
  const panelProps = { shipment: s, mode: ops ? "ops" : "customer", onChange: reload };

  return (
    <div>
      <PageHeader
        crumbs={[{ label: t("shipments.detail.back"), to: base }, { label: s.reference }]}
        title={<span className="inline-flex max-w-full flex-wrap items-center gap-2"><span className="font-mono whitespace-nowrap">{s.reference}</span><StatusBadge group="status" value={s.status} /><DemoTag show={ops && s.demo} /></span>}
        meta={(
          <>
            <ModeLabel mode={s.mode} />
            <span aria-hidden className="text-ink-tertiary">·</span>
            <RouteText origin={s.origin} destination={s.destination} short={false} />
            {ops && s.customer_name && (
              <>
                <span aria-hidden className="text-ink-tertiary">·</span>
                <Link to={`/ops/customers/${s.company_id}`} className="hover:underline">{s.customer_name}</Link>
              </>
            )}
            {s.references?.customer_ref && (
              <>
                <span aria-hidden className="text-ink-tertiary">·</span>
                <span className="font-mono text-[13px]">{s.references.customer_ref}</span>
              </>
            )}
          </>
        )}
        actions={(
          <>
            <DataAge q={q} />
            {ops && <OpsActionBar s={s} onChanged={reload} />}
          </>
        )}
      />

      <DecisionStrip s={s} />
      <IssueBanner s={s} ops={ops} onChanged={reload} onMessage={() => setTab("messages")} />

      <div role="tablist" aria-label={s.reference} className="mb-4 flex gap-1 overflow-x-auto no-scrollbar border-b border-[color:var(--border-subtle)]">
        {TABS.map((k) => (
          <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setTab(k)}
            className={cn("-mb-px whitespace-nowrap border-b-2 px-2.5 pb-2 pt-1 text-sm",
              tab === k ? "border-[color:var(--text-primary)] font-medium text-foreground" : "border-transparent text-ink-secondary hover:text-foreground")}>
            {tabLabels[k]}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
          <div className="min-w-0">
            <Section title={t("shipments.section.milestones")}>
              <MilestoneTimeline milestones={s.milestones} legs={s.legs} />
              <div className="mt-4"><Forecast prediction={s.eta_prediction} /></div>
            </Section>
            <Section title={t("shipments.section.legs")}>
              <LegsTable shipment={s} ops={ops} onChanged={reload} />
            </Section>
            <Section title={t("shipments.section.map")}>
              <div className="relative">
                <ShipmentMap embedded scope={ops ? "ops" : "customer"} initialSelectedId={s.id} height={360} initialView={mapView(s)} />
                <IdentityFlag identity={s.position?.identity} className="absolute left-3 top-3 z-10 max-w-sm shadow-sm" />
              </div>
              <p className="mt-1.5 text-xs text-ink-tertiary">{t("shipments.map_note")}</p>
            </Section>
          </div>
          <aside className="min-w-0">
            {ops && <InternalNotes s={s} />}
            <CargoFacts s={s} />
            <ReferencesFacts s={s} canEdit onSaved={(refs) => q.setData((d) => ({ ...d, references: refs }))} />
            <PartiesFacts s={s} />
            <TermsFacts s={s} ops={ops} />
            {ops && <CompanyChecks s={s} />}
            {ops && <TrackingFeeds s={s} />}
            <LaneLinks s={s} ops={ops} />
          </aside>
        </div>
      )}
      {tab === "documents" && <Panel><TradeDocumentsPanel {...v3Props} /><DocumentsPanel {...v3Props} /></Panel>}
      {tab === "customs" && <Panel><CustomsPanel {...v3Props} /></Panel>}
      {tab === "invoices" && <Panel><InvoicesPanel {...panelProps} /></Panel>}
      {tab === "messages" && <Panel><MessagesPanel {...panelProps} /></Panel>}
      {tab === "exceptions" && <Panel><ExceptionsPanel {...panelProps} /></Panel>}
      {tab === "history" && (
        <Section title={t("shipments.section.history")}>
          <EventHistory events={s.tracking_events} />
        </Section>
      )}
    </div>
  );
}

/** The AIS source and the booking disagree on the vessel's name or IMO for this MMSI: say so, never pick one silently. */
function identityText(t, identity) {
  if (identity?.state !== "disagree") return null;
  const who = (v) => [v?.name, v?.imo ? `IMO ${v.imo}` : null].filter(Boolean).join(", ") || "—";
  return t("shipments.ais_identity.body", { mmsi: identity.mmsi || "—", provider: identity.provider || "AIS",
    reported: who(identity.reported), booked: who(identity.booked) });
}

function IdentityFlag({ identity, className }) {
  const { t } = useT();
  const text = identityText(t, identity);
  if (!text) return null;
  return (
    <div role="status" className={cn("flex gap-2 rounded-lg border border-[color:var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-2 text-sm", className)}>
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning-fg" aria-hidden />
      <div className="min-w-0">
        <p className="font-medium">{t("shipments.ais_identity.title")}</p>
        <p className="text-ink-secondary">{text}</p>
      </div>
    </div>
  );
}

/** Four label/value pairs that answer "when, what next, who, how sure". Not KPI tiles. */
function DecisionStrip({ s }) {
  const { t, locale } = useT();
  const delivered = s.status === "delivered";
  const next = (s.milestones || []).filter((m) => !m.at && m.planned).sort((a, b) => new Date(a.planned) - new Date(b.planned))[0];
  const late = s.delay_days > 0;
  const overdue = !delivered && s.eta && new Date(s.eta).getTime() < Date.now();
  const main = [...(s.legs || [])].filter((l) => !["pickup", "delivery"].includes(l.role)).sort((a, b) => (b.distance_km || 0) - (a.distance_km || 0))[0] || (s.legs || [])[0];
  const vehicle = main?.vessel?.name || main?.flight || main?.train?.number || main?.truck_plate;
  const pos = s.position || {};
  const live = ["ais", "gps", "carrier_api"].includes(pos.source);
  const Item = ({ label, children, sub, tone }) => (
    <div className="min-w-0">
      <dt className="text-xs text-ink-tertiary">{label}</dt>
      <dd className={cn("mt-0.5 truncate text-md tabular", tone === "danger" && "text-danger-fg", tone === "warning" && "text-warning-fg")}>{children}</dd>
      {sub && <dd className="truncate text-xs text-ink-tertiary">{sub}</dd>}
    </div>
  );
  return (
    <dl className="mb-4 grid grid-cols-2 gap-x-6 gap-y-3 rounded-xl border border-[color:var(--border-subtle)] px-4 py-3 lg:grid-cols-4">
      <Item label={delivered ? t("shipments.detail.delivered") : t("shipments.detail.eta")}
        tone={overdue ? "danger" : late ? "warning" : null}
        sub={delivered ? t("shipments.eta_actual")
          : overdue ? t("shipments.past_eta")
          : late ? `${t("shipments.late_by", { n: s.delay_days })} · ${t("shipments.detail.planned_eta", { date: date(s.eta_planned, locale) })}`
          : `${t("shipments.eta_est")} · ${t("shipments.detail.on_plan")}`}>
        {dateTime(delivered ? s.delivered_at || s.eta : s.eta, locale)}
      </Item>
      <Item label={t("shipments.detail.next")} sub={next ? `${dateTime(next.planned, locale)} (${t("shipments.ms.estimate")})` : null}>
        {next ? `${msLabel(t, next)}${next.location?.name ? ` · ${next.location.name}` : ""}` : <span className="text-ink-tertiary">{t("shipments.detail.no_next")}</span>}
      </Item>
      <Item label={t("shipments.detail.carrier")} sub={vehicle || null}>
        {main?.carrier || <span className="text-ink-tertiary">{t("shipments.no_carrier")}</span>}
      </Item>
      <Item label={t("shipments.detail.tracking")} tone={live && pos.identity?.state === "disagree" ? "warning" : null}
        sub={live ? (pos.identity?.state === "disagree" ? t("shipments.ais_identity.title") : relTime(pos.updated_at, locale)) : t("shipments.no_tracking_why")}>
        {live ? <span title={identityText(t, pos.identity) || undefined}>{sourceLabel(t, pos.source)}</span>
          : <span title={t("shipments.no_tracking_why")}>{delivered ? sourceLabel(t, "manual") : t("shipments.no_tracking")}</span>}
      </Item>
    </dl>
  );
}

/** One banner above the timeline: cause, impact on ETA, next action. */
function IssueBanner({ s, ops, onChanged, onMessage }) {
  const { t, locale } = useT();
  const { can, user } = useAuth();
  const holds = s.holds || [];
  const exceptions = useApi(`/exceptions?shipment_id=${s.id}&status=open`, { enabled: !!s.has_exception || s.status === "exception" });
  const actual = exceptions.data?.items?.[0];
  const exception = s.status === "exception" || s.has_exception;
  if (!holds.length && !exception) return null;
  const exNote = actual?.title || actual?.description || s.exception_note || [...(s.milestones || [])].reverse().find((m) => m.code === "exception" && m.at)?.note;
  const release = async (type) => {
    await apiDelete(`/shipments/${s.id}/holds/${type}`);
    onChanged?.();
  };
  const perms = new Set(user?.permissions || []);
  return (
    <div role="alert" className="mb-4 rounded-xl border border-[color:var(--border-subtle)] px-4 py-3">
      <ul className="flex flex-col gap-2.5">
        {exception && (
          <li className="flex gap-2.5">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-danger-fg" aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">{t("shipments.banner.exception")}</p>
              <p className="text-sm text-ink-secondary">{exNote || t("review.exception_missing")}</p>
              <Link className="text-sm underline" to={ops ? `/ops/exceptions?focus=${actual?.id || ""}&shipment=${s.id}` : `/portal/shipments/${s.id}?tab=exceptions`}>{t("review.exception_open")}</Link>
            </div>
          </li>
        )}
        {holds.map((h) => (
          <li key={h.type} className="flex gap-2.5">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning-fg" aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">{t("shipments.banner.hold", { type: holdLabel(t, h.type) })}</p>
              <p className="text-sm text-ink-secondary">{h.reason || t("shipments.banner.no_note")}</p>
              <p className="text-xs text-ink-tertiary">
                {t("shipments.banner.since", { date: dateTime(h.since, locale), by: h.by || "—" })}
                {DELIVERY_BLOCKING.has(h.type) ? ` · ${t("shipments.banner.blocks")}` : ""}
              </p>
            </div>
            {ops && (h.type === "sanctions" ? perms.has("kyc.approve") : can("shipments.write") || can("customs.review")) && (
              <Button size="xs" variant="outline" onClick={() => release(h.type)}>{t("shipments.banner.release")}</Button>
            )}
          </li>
        ))}
      </ul>
      <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 border-t border-[color:var(--border-subtle)] pt-2.5">
        <p className="text-xs text-ink-secondary">
          {s.delay_days > 0 ? t("shipments.banner.impact_late", { n: s.delay_days }) : t("shipments.banner.impact_none")}
          {!ops && ` ${t("shipments.banner.next_customer")}`}
        </p>
        {!ops && <Button size="xs" variant="outline" onClick={onMessage}><MessageSquare /> {t("shipments.banner.message")}</Button>}
      </div>
    </div>
  );
}

/** Links to the other lanes' pages filtered to this shipment. */
function LaneLinks({ s, ops }) {
  const { t } = useT();
  const enc = encodeURIComponent;
  const links = ops
    ? [["shipments.tab.invoices", `/ops/billing?shipment=${enc(s.id)}&q=${enc(s.reference)}`],
      ["shipments.tab.messages", `/ops/messages?shipment=${enc(s.id)}&q=${enc(s.reference)}`],
      ["shipments.tab.exceptions", `/ops/exceptions?shipment=${enc(s.id)}&q=${enc(s.reference)}`]]
    : [["shipments.tab.invoices", `/portal/invoices?shipment=${enc(s.id)}&q=${enc(s.reference)}`],
      ["shipments.tab.messages", `/portal/messages?shipment=${enc(s.id)}&q=${enc(s.reference)}`]];
  return (
    <Section title={t("shipments.section.lanes")}>
      <ul className="flex flex-col gap-1">
        {links.map(([k, to]) => (
          <li key={k}>
            <Link to={to} className="inline-flex items-center gap-1.5 text-sm text-ink-accent hover:underline">
              {t(k)} <ExternalLink className="size-3.5" aria-hidden />
            </Link>
          </li>
        ))}
      </ul>
    </Section>
  );
}
