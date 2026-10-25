// Shared filing workbench. Flow-specific endpoints stay explicit; no synthetic statuses or messages.
import React, { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageHeader, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, LoadingRows, EmptyState, Freshness } from "@/components/app/states";
import { apiPost, fileUrl, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { CustomsHubTabs, ApiMissing, bi, isMissingApi, MessageLog, DecisionCard, FlowTimeline } from "./extCommon";
import ControlsDeclaration from "./ControlsDeclaration";

export const FLOWS = {
  export: { api: "/customs-export", path: ["draft", "ready", "submitted", "accepted", "released", "exited"] },
  transit: { api: "/customs-transit", path: ["draft", "ready", "submitted", "accepted", "released", "arrived", "unloading", "closed"] },
  ens: { api: "/customs-ens", path: ["draft", "submitted", "registered", "assessment_complete", "arrived"] },
};
export const FLOW_STATES = { draft: "pending", approved: "scheduled", ready: "scheduled", submitted: "submitted", accepted: "in-progress", registered: "in-progress", released: "success", exited: "success", arrived: "success", closed: "success", unloading: "in-progress", rejected: "failed", under_control: "in-review", invalidated: "expired", assessment_complete: "success", referral: "flagged", do_not_load: "failed" };
export function filingMessages(messages, base, id) {
  return (messages || []).map(m => ({ id: m.id, dir: m.direction, type: m.type,
    at: m.at || m.created_at, valid: m.validation?.valid ?? m.xsd_valid, errors: m.validation?.errors || m.xsd_errors || [], schema: m.validation?.schema_version || m.schema,
    mrn: m.mrn, xmlHref: m.id ? fileUrl(`${base}/${id}/messages/${m.id}/xml`) : null }));
}
function FilingDecision({ decl }) {
  const q = useApi(decl.decision_id ? `/autopilot/decisions/${decl.decision_id}` : null);
  return <>{q.error && <ErrorState compact error={q.error} onRetry={q.reload} />}<DecisionCard decision={q.data || decl.agent_decision || decl.decision} /></>;
}
export function filingItems(decl, kind) {
  if (kind !== "ens") return (decl.items || []).map(it => ({ ...it, code: it.taric_code || it.cn_code || it.hs_code, mass: it.net_kg ?? it.gross_mass }));
  return (decl.ens_data?.consignmentMasterLevel?.consignmentHouseLevel || []).flatMap(h => (h.goodsItem || []).map(it => ({ seq: it.goodsItemNumber, description: it.commodity?.descriptionOfGoods, code: it.commodity?.commodityCode?.harmonizedSystemSubHeadingCode, mass: it.weight?.grossMass })));
}
export function FilingDetail({ decl, kind }) {
  const { t, locale } = useT();
  const cfg = FLOWS[kind];
  const label = status => t(`cx.flow.st.${status}`);
  return <div className="grid gap-x-8 lg:grid-cols-[minmax(0,1fr)_300px]">
    <div className="min-w-0">
      {(decl.submission?.sandbox || decl.messages?.some(m => m.sandbox)) && <p className="mb-4 rounded-md bg-secondary px-3 py-2 text-sm">{t("cx.flow.sandbox")}</p>}
      <Section title="MRN"><p className="text-sm font-mono">{decl.mrn || t("cx.flow.mrn_missing")}</p></Section>
      <Section title={t("cx.flow.items")}><ul className="divide-y divide-border">{filingItems(decl, kind).map((it, i) => <li key={it.seq || i} className="py-2 text-sm"><p>#{it.seq || i + 1} · {it.description}</p><p className="text-xs text-ink-secondary font-mono">{[it.code, it.origin_country, it.mass == null ? null : `${it.mass} kg`].filter(Boolean).join(" · ")}</p></li>)}</ul></Section>
      <Section title={t("cx.msg.title")}><MessageLog messages={filingMessages(decl.messages, cfg.api, decl.id)} /></Section>
    </div>
    <aside className="min-w-0">
      <Section title={t("cx.dec.title")}><FilingDecision decl={decl} /></Section>
      <Section title={t("cx.flow.timeline")}><FlowTimeline history={(decl.history?.length ? decl.history : decl.created_at ? [{ status: "draft", at: decl.created_at }] : []).map(h => ({ ...h, status: h.to || h.status, note: bi(locale, h, "note"), by: typeof h.by === "object" ? h.by.name : h.by }))} path={cfg.path} status={decl.status} label={label} /></Section>
      <Section title={t("cx.ctl.title")}><ControlsDeclaration id={decl.id} kind={kind} /></Section>
    </aside>
  </div>;
}
export default function CustomsFlow({ kind }) {
  const { id } = useParams();
  const { t, locale } = useT();
  const navigate = useNavigate();
  const { user } = useAuth();
  const cfg = FLOWS[kind];
  const q = useApi(`${cfg.api}${id ? `/${id}` : ""}`);
  const shipments = useApi("/shipments?limit=200", { enabled: !id });
  const [shipment, setShipment] = useState("");
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const decl = id ? q.data : null;
  const rows = Array.isArray(q.data) ? q.data : q.data?.items || [];
  const statusLabel = d => bi(locale, d, "status_label") || t(`cx.flow.st.${d.status}`);
  async function act(action) {
    setBusy(true); setError(null);
    try {
      const d = await apiPost(`${cfg.api}${id ? `/${id}/${action}` : ""}`, id ? {} : { shipment_id: shipment });
      if (!id) navigate(`/ops/customs/${kind}/${d.id}`); else await q.reload();
    } catch (e) { setError(e); } finally { setBusy(false); }
  }
  return <div className="min-w-0">
    <PageHeader title={decl?.lrn || t(`cx.hub.${kind}`)} crumbs={[{ label: t("nav.customs"), to: "/ops/customs" }, { label: t(`cx.hub.${kind}`), to: id ? `/ops/customs/${kind}` : undefined }, ...(id ? [{ label: decl?.lrn || id }] : [])]}
      meta={<><Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />{decl && <StatusBadge size="sm" state={FLOW_STATES[decl.status]} label={statusLabel(decl)} />}{decl?.shipment_id && <Link to={`/ops/shipments/${decl.shipment_id}`}>{decl.shipment_ref || t("cx.flow.shipment")}</Link>}</>} />
    <CustomsHubTabs active={kind} />
    {isMissingApi(q.error) ? <ApiMissing /> : <>
      {q.error && <ErrorState error={q.error} onRetry={q.reload} />}
      {error && <ErrorState error={error} compact />}
      {q.loading && !q.data && <LoadingRows rows={6} />}
      {!id && q.data && <>
        <form onSubmit={e => { e.preventDefault(); act(); }} className="mb-4 flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1 text-xs"><span>{t("cx.flow.shipment")}</span><select required className="h-8 max-w-full rounded-md border bg-background px-2 text-sm" value={shipment} onChange={e => setShipment(e.target.value)}><option value="">{t("cx.flow.select")}</option>{(shipments.data?.items || (Array.isArray(shipments.data) ? shipments.data : [])).map(s => <option key={s.id} value={s.id}>{s.reference}</option>)}</select></label>
          <Button type="submit" disabled={busy || !shipment}>{t(busy ? "state.saving" : "cx.flow.create")}</Button>
          <label className="ml-auto"><span className="sr-only">{t("action.search")}</span><Input value={search} onChange={e => setSearch(e.target.value)} placeholder={t("cx.flow.reference")} /></label>
        </form>
        {!rows.length ? <EmptyState title={t("cx.flow.empty")} /> : <ul className="divide-y divide-border">{rows.filter(d => [d.lrn, d.mrn, d.shipment_ref].some(v => String(v || "").toLowerCase().includes(search.toLowerCase()))).map(d => <li key={d.id}><Link className="flex flex-wrap justify-between gap-2 py-3 hover:bg-hover" to={`/ops/customs/${kind}/${d.id}`}><span className="min-w-0"><span className="font-mono text-sm">{d.lrn || d.id}</span><span className="block text-xs text-ink-secondary">{[d.shipment_ref, d.mrn].filter(Boolean).join(" · ")}</span></span><StatusBadge size="sm" state={FLOW_STATES[d.status]} label={statusLabel(d)} /></Link></li>)}</ul>}
      </>}
      {decl && <>
        {<div className="mb-4 flex gap-2">{kind !== "ens" && ["draft", "rejected"].includes(decl.status) && user?.permissions?.includes("customs.review") && <Button disabled={busy} variant="outline" onClick={() => act("ready")}>{t("cx.flow.ready")}</Button>}{["ready", "approved", ...(kind === "ens" ? ["draft"] : [])].includes(decl.status) && user?.permissions?.includes("customs.file") && <Button disabled={busy} onClick={() => act("file")}>{t("cx.flow.file")}</Button>}</div>}
        <FilingDetail decl={decl} kind={kind} />
      </>}
    </>}
  </div>;
}
