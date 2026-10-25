// Sourcing Autopilot (Hankinta-autopilotti). Decision: is the autopilot running inside its limits, which partners
// answered, and which negotiated rates reached the rate table?
import React, { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ArrowDownLeft, ArrowUpRight, ExternalLink, Inbox, Play, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { KV, PageHeader, Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { EmptyState, Freshness, Loadable } from "@/components/app/states";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { Dash, MobileRow, NativeSelect, Tip, useIsNarrow } from "@/features/quotes/ops/common";
import { domainError } from "@/features/domainErrors";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { money, num, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Agents, Allocations, Scorecards } from "./Procurement";

const RFQ_STATE = {
  discovered: "in-progress", queued: "scheduled", awaiting_reply: "submitted", reply_received: "in-review",
  clarification_sent: "submitted", needs_review: "pending", accepted: "success", pending: "in-review",
  declined: "expired", declined_by_partner: "expired", opted_out: "expired", expired: "expired", cancelled: "expired",
  email_failed: "failed", rejected: "failed", revoked: "expired", contact_unavailable: "expired", outside_policy: "flagged",
  logged: "expired", sent: "submitted", failed: "failed", held_limit: "scheduled", blocked: "flagged",
};
const CAND_STATE = { verification: "in-review", new: "in-progress", contactable: "scheduled", contacted: "submitted", partner: "success", declined: "expired",
  opted_out: "expired", excluded: "failed" };
const MSG_STATE = { sent: "success", logged: "expired", failed: "failed", received: "submitted", unmatched: "flagged",
  sender_mismatch: "flagged", unauthenticated: "flagged", counteroffered: "submitted", accepted: "success", declined: "expired",
  declined_by_partner: "expired", opted_out: "expired", clarification_sent: "submitted", needs_review: "pending",
  reply_received: "in-review", closed: "expired", pending: "in-review" };

const eur = (c, locale) => money(c, locale, { decimals: 2 });
/** dd.mm.yyyy (Finnish) / dd/mm/yyyy, with hh:mm for timestamps. */
export function stamp(iso, locale, withTime = true) {
  if (!iso) return "—";
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00Z` : iso);
  if (Number.isNaN(d.getTime())) return "—";
  const tag = locale === "fi" ? "fi-FI" : "en-GB";
  const day = d.toLocaleDateString(tag, { day: "2-digit", month: "2-digit", year: "numeric" });
  return withTime ? `${day} ${d.toLocaleTimeString(tag, { hour: "2-digit", minute: "2-digit" })}` : day;
}
const tr = (t, key, fallback) => { const s = t(key); return s === key ? fallback : s; };
const lastOffer = (o) => [...(o.offers || [])].reverse().find((x) => x && x.cost_cents)?.cost_cents ?? null;

/** road:FI>SE -> "Maantie FI → SE" */
function laneLabel(lane, t) {
  const m = /^(\w+):([A-Z]{2})>([A-Z]{2})$/.exec(lane || "");
  return m ? `${t(`enum.mode.${m[1]}`)} ${m[2]} → ${m[3]}` : (lane || "—");
}

export default function Sourcing() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canRun = can("settings.write");
  const canRates = can("rates.write");
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "rfqs";
  const openId = params.get("rfq");
  const q = useApi("/partner-network", { pollMs: 30000 });
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [confirmStop, setConfirmStop] = useState(false);

  const setParam = (k, v) => {
    const next = new URLSearchParams(params);
    if (!v) next.delete(k); else next.set(k, v);
    setParams(next, { replace: true });
  };
  const act = async (kind) => {
    setBusy(kind); setError(""); setNote("");
    try {
      if (kind === "run") {
        const r = await apiPost("/partner-network/run", {});
        const rfqs = (r.lanes || []).reduce((a, l) => a + (l.rfqs || []).length, 0);
        setNote(r.status === "ran" ? t("src.cycle_done", { rfqs, inbox: r.inbox?.processed || 0 })
          : r.status === "needs_policy" ? t("src.cycle_policy") : t("src.cycle_off"));
      } else if (kind === "poll") {
        const r = await apiPost("/partner-network/poll", {});
        setNote(t("src.cycle_done", { rfqs: 0, inbox: r.processed || 0 }));
      } else if (kind === "stop") {
        const r = await apiPost("/partner-network/stop", {});
        setConfirmStop(false);
        setNote(t("src.stopped", { n: r.cancelled || 0 }));
        window.dispatchEvent(new Event("huo.autonomy"));
      }
      await q.reload();
    } catch (e) { setError(domainError(e, t)); } finally { setBusy(""); }
  };

  const st = q.data?.status;
  return (
    <div>
      <PageHeader
        title={t("src.title")}
        meta={st && <>
          <StatusBadge state={st.enabled ? "success" : "expired"} label={st.env_kill ? t("src.env_kill") : t(st.enabled ? "src.on" : "src.off")} />
          <span className="tabular">{t("src.sends", { n: num(st.sends_today, locale), limit: num(st.limit, locale) })}</span>
          <span>{st.last_cycle?.finished_at ? t("src.last_cycle", { at: stamp(st.last_cycle.finished_at, locale) }) : t("src.never")}</span>
          <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
        </>}
        actions={canRun && <>
          <Button variant="outline" disabled={!!busy} onClick={() => act("poll")}><Inbox />{t("src.poll")}</Button>
          <Button disabled={!!busy || !st?.enabled} onClick={() => act("run")}><Play />{t(busy === "run" ? "src.running" : "src.run")}</Button>
          {st?.switch && <Button variant="outline" className="text-danger-fg" disabled={!!busy} onClick={() => setConfirmStop(true)}><Square />{t("src.stop")}</Button>}
        </>}
      />
      <p className="-mt-2 mb-3 max-w-[75ch] text-sm text-ink-secondary">{t("src.lead")}</p>
      {st && !st.enabled && (
        <p role="status" className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg p-3 text-sm status-pending">
          {t("src.off_hint")} <Link to="/ops/settings" className="font-medium underline">{t("src.turn_on")}</Link>
        </p>
      )}
      {st && st.outreach_live === false && <p role="status" className="mb-3 text-sm text-ink-secondary">{t("src.outreach_held")}</p>}
      {st?.sandbox_domain && <p className="mb-3 text-xs text-ink-tertiary">{t("src.sandbox", { domain: st.sandbox_domain })}</p>}
      {note && <p role="status" className="mb-3 text-sm">{note}</p>}
      {error && <p role="alert" className="mb-3 text-sm text-destructive">{error}</p>}

      <Tabs value={tab} onValueChange={(v) => setParam("tab", v === "rfqs" ? null : v)}>
        <div className="overflow-x-auto"><TabsList>
          <TabsTrigger value="rfqs">{t("src.tab.rfqs")}{q.data && <span className="tabular text-ink-tertiary">{q.data.items.length}</span>}</TabsTrigger>
          <TabsTrigger value="candidates">{t("src.tab.candidates")}{st && <span className="tabular text-ink-tertiary">{st.candidates}</span>}</TabsTrigger>
          <TabsTrigger value="log">{t("src.tab.log")}</TabsTrigger>
          <TabsTrigger value="optouts">{t("src.tab.optouts")}{st && <span className="tabular text-ink-tertiary">{st.optouts}</span>}</TabsTrigger>
          <TabsTrigger value="allocations">{t("prc.tab.allocations")}</TabsTrigger>
          <TabsTrigger value="agents">{t("prc.tab.agents")}</TabsTrigger>
          <TabsTrigger value="scorecards">{t("prc.tab.scorecards")}</TabsTrigger>
        </TabsList></div>
        <TabsContent value="rfqs"><Loadable q={q}>{(d) => <RfqList rows={d.items} onOpen={(id) => setParam("rfq", id)} />}</Loadable></TabsContent>
        <TabsContent value="candidates">{tab === "candidates" && <Candidates lanes={q.data?.settings?.partner_approved_lanes || []} canRun={canRun} />}</TabsContent>
        <TabsContent value="log">{tab === "log" && <MessageLog onOpen={(id) => setParam("rfq", id)} />}</TabsContent>
        <TabsContent value="optouts">{tab === "optouts" && <OptOuts canWrite={canRun} onChanged={q.reload} />}</TabsContent>
        <TabsContent value="allocations">{tab === "allocations" && <Allocations canWrite={canRates} />}</TabsContent>
        <TabsContent value="agents">{tab === "agents" && <Agents canWrite={canRates} />}</TabsContent>
        <TabsContent value="scorecards">{tab === "scorecards" && <Scorecards />}</TabsContent>
      </Tabs>

      <Thread id={openId} onClose={() => setParam("rfq", null)} />

      <Dialog open={confirmStop} onOpenChange={setConfirmStop}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t("src.stop_title")}</DialogTitle><DialogDescription>{t("src.stop_body")}</DialogDescription></DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmStop(false)}>{t("action.cancel")}</Button>
            <Button variant="destructive" disabled={busy === "stop"} onClick={() => act("stop")}><Square />{t("src.stop")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function RfqStatus({ value, size }) {
  const { t } = useT();
  return <StatusBadge size={size} state={RFQ_STATE[value] || "expired"} label={tr(t, `src.status.${value}`, value)} />;
}

function RfqList({ rows, onOpen }) {
  const { t, locale } = useT();
  const narrow = useIsNarrow();
  if (!rows.length) return <EmptyState title={t("src.empty.rfqs")} body={t("src.empty.rfqs_body")} />;
  if (narrow) {
    return (
      <ul className="divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
        {rows.map((o) => (
          <MobileRow key={o.id} onClick={() => onOpen(o.id)} primary={o.carrier_name} status={<RfqStatus value={o.status} size="sm" />}
            secondary={`${o.ref || "—"} · ${laneLabel(o.lane, t)} · ${relTime(o.updated_at, locale)}`}
            right={lastOffer(o) != null ? eur(lastOffer(o), locale) : "—"}
            rightSub={o.last_counter_cents ? `${t("src.col.counter")} ${eur(o.last_counter_cents, locale)}` : null} />
        ))}
      </ul>
    );
  }
  const columns = [
    { key: "ref", header: t("src.col.ref"), className: "pl-3 font-mono text-[13px]", headClassName: "pl-3", cell: (o) => o.ref || "—" },
    { key: "carrier_name", header: t("src.col.partner"), className: "max-w-[260px]",
      cell: (o) => <span className="flex min-w-0 flex-col"><span className="truncate">{o.carrier_name}</span>
        <span className="truncate text-xs text-ink-tertiary">{[o.country, o.business_id].filter(Boolean).join(" · ") || o.domain}</span></span> },
    { key: "lane", header: t("src.col.lane"), cell: (o) => laneLabel(o.lane, t) },
    { key: "status", header: t("src.col.status"), cell: (o) => <RfqStatus value={o.status} /> },
    { key: "offer", header: t("src.col.offer"), align: "right", sort: lastOffer, className: "tabular",
      cell: (o) => (lastOffer(o) != null ? eur(lastOffer(o), locale) : <Dash />) },
    { key: "last_counter_cents", header: t("src.col.counter"), align: "right", className: "tabular",
      cell: (o) => (o.last_counter_cents ? eur(o.last_counter_cents, locale) : <Dash />) },
    { key: "rounds", header: t("src.col.rounds"), align: "right", className: "tabular", cell: (o) => num(o.rounds || 0, locale) },
    { key: "updated_at", header: t("src.col.updated"), className: "whitespace-nowrap text-ink-secondary pr-3", headClassName: "pr-3",
      cell: (o) => <span title={stamp(o.updated_at, locale)}>{relTime(o.updated_at, locale)}</span> },
  ];
  return (
    <div className="overflow-x-auto rounded-xl border border-[color:var(--border-subtle)]">
      <DataTable columns={columns} rows={rows} dense pageSize={25} pageLabel="src.page" onRowClick={(o) => onOpen(o.id)} className="min-w-[900px]"
        defaultSort={{ key: "updated_at", dir: "desc" }} />
    </div>
  );
}

function Parsed({ p }) {
  const { t, locale } = useT();
  if (!p) return null;
  const method = p.method || "rules";
  return (
    <div className="mt-2 rounded-lg bg-secondary p-3 text-sm">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-ink-secondary">
        <span className="font-medium text-foreground">{t("src.parsed")}</span>
        {method.includes("ai") ? <AgentMark label={tr(t, `src.method.${method}`, method)} /> : <span>{t("src.parsed_by", { method: tr(t, `src.method.${method}`, method) })}</span>}
        {p.intent && p.intent !== "offer" && <span>· {tr(t, `src.msg.${p.intent === "accept_counter" ? "accepted" : p.intent}`, p.intent)}</span>}
      </div>
      <KV cols={3} items={[
        { label: t("src.price"), value: p.cost_cents ? eur(p.cost_cents, locale) : null, className: "tabular" },
        { label: t("src.base"), value: p.base_cents ? eur(p.base_cents, locale) : null, className: "tabular" },
        { label: t("src.transit"), value: p.transit_days ? t("src.days", { n: p.transit_days }) : null },
        { label: t("src.valid_to"), value: p.valid_to ? stamp(p.valid_to, locale, false) : null },
        { label: t("src.surcharges"), value: (p.surcharges || []).length ? (p.surcharges || []).map((s) =>
          `${s.label || s.code} ${s.pct != null ? `${num(s.pct, locale, 2)} %` : s.amount_cents ? eur(s.amount_cents, locale) : ""}`.trim()).join(" · ") : null },
      ]} />
      {p.missing?.length > 0 && <p className="mt-2 text-xs text-warning-fg">{t("src.missing", { list: p.missing.join(", ") })}</p>}
      {p.conflict && <p className="mt-2 text-xs text-warning-fg">{t("src.conflict", { a: eur(p.conflict.rules_cents, locale), b: eur(p.conflict.ai_cents, locale) })}</p>}
    </div>
  );
}

function Thread({ id, onClose }) {
  const { t, locale } = useT();
  const q = useApi(id ? `/partner-network/${id}/thread` : null, { enabled: !!id });
  const o = q.data?.outreach;
  return (
    <Sheet open={!!id} onOpenChange={(v) => { if (!v) onClose(); }}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-y-auto p-0 sm:max-w-[720px]">
        <SheetHeader className="border-b border-[color:var(--border-subtle)] px-5 py-4 text-left">
          <SheetTitle className="text-md font-medium">{o ? `${o.ref || ""} · ${o.carrier_name}` : t("src.thread")}</SheetTitle>
          <SheetDescription className="flex flex-wrap items-center gap-2 text-xs text-ink-tertiary">
            {o && <><span>{laneLabel(o.lane, t)}</span><RfqStatus value={o.status} size="sm" /></>}
          </SheetDescription>
        </SheetHeader>
        <div className="px-5 py-4">
          <Loadable q={q}>{(d) => <>
            <KV items={[
              { label: t("src.col.contact"), value: d.outreach.contact_email, mono: true },
              { label: t("src.col.to"), value: d.outreach.send_to && d.outreach.send_to !== d.outreach.contact_email ? `${d.outreach.send_to} (${t("src.sandbox_flag")})` : d.outreach.send_to, mono: true },
              { label: t("src.col.bid"), value: d.outreach.business_id, mono: true },
              { label: t("src.policy"), value: d.outreach.limit_cents ? t("src.limit", { v: eur(d.outreach.limit_cents, locale) }) : null,
                hint: d.outreach.rounds ? `${t("src.col.rounds")} ${d.outreach.rounds}` : null },
              { label: t("src.evidence"), value: (d.outreach.sources || []).length ? <SourceLinks sources={d.outreach.sources} /> : null },
              { label: t("src.col.source"), value: d.outreach.contact_source_url ? <a className="break-all text-ink-accent hover:underline" href={d.outreach.contact_source_url} target="_blank" rel="noreferrer">{d.outreach.contact_source_url}</a> : null },
            ]} />
            {d.rate && (
              <Section title={t("src.rate")} className="mt-5">
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                  <StatusBadge state={d.rate.status === "accepted" ? "success" : "in-review"} label={tr(t, `src.status.${d.rate.status}`, d.rate.status)} />
                  <span className="tabular font-medium">{eur(d.rate.all_in_cost_cents ?? d.rate.unit_cost_cents, locale)}</span>
                  <span className="text-ink-secondary">{stamp(d.rate.valid_from, locale, false)} – {stamp(d.rate.valid_to, locale, false)}</span>
                  <span className="text-ink-secondary">{t("src.days", { n: d.rate.transit_days })}</span>
                  <Link className="ml-auto text-ink-accent hover:underline" to={`/ops/rates?carrier=${d.rate.carrier_id}`}>{t("src.rate_open")}</Link>
                </div>
              </Section>
            )}
            <Section title={t("src.thread")} className="mt-5">
              <ol className="flex flex-col gap-3">
                {d.messages.map((m) => (
                  <li key={m.id} className={cn("rounded-lg border border-[color:var(--border-subtle)] p-3", m.direction === "in" && "bg-surface")}>
                    <div className="flex flex-wrap items-center gap-2 text-xs text-ink-secondary">
                      {m.direction === "in" ? <ArrowDownLeft className="size-3.5" aria-label={t("src.in")} /> : <ArrowUpRight className="size-3.5" aria-label={t("src.out")} />}
                      <span className="font-medium text-foreground">{m.direction === "in" ? t("src.kind.reply") : tr(t, `src.kind.${m.kind}`, m.kind)}</span>
                      {m.amount_cents ? <span className="tabular">{eur(m.amount_cents, locale)}</span> : null}
                      <span>{stamp(m.at, locale)}</span>
                      {m.status && <StatusBadge size="sm" state={MSG_STATE[m.status] || "expired"} label={tr(t, `src.msg.${m.status}`, m.status)} />}
                    </div>
                    <p className="mt-1 break-words text-sm">{m.subject}</p>
                    <p className="text-xs text-ink-tertiary break-all">{m.direction === "in" ? m.from : m.to}{m.sandbox && m.to_original ? ` · ${t("src.real", { email: m.to_original })}` : ""}</p>
                    <details className="mt-1 text-sm"><summary className="cursor-pointer text-xs text-ink-secondary">{t("src.col.kind")}</summary>
                      <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words font-sans text-[13px] text-ink-secondary">{m.text}</pre></details>
                    {m.direction === "in" && <Parsed p={m.parsed} />}
                  </li>
                ))}
              </ol>
            </Section>
          </>}</Loadable>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function SourceLinks({ sources }) {
  const { t } = useT();
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-1">
      {sources.map((s, i) => (
        <a key={i} href={s.url} target="_blank" rel="noreferrer" title={s.detail} className="inline-flex items-center gap-1 text-ink-accent hover:underline">
          {tr(t, `src.source.${s.source}`, s.source)}<ExternalLink className="size-3" aria-hidden />
        </a>
      ))}
    </span>
  );
}

function Candidates({ lanes, canRun }) {
  const { t, locale } = useT();
  const narrow = useIsNarrow();
  const [lane, setLane] = useState(lanes[0] || null);
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const qs = new URLSearchParams({ limit: "500" });
  if (lane) qs.set("lane", lane);
  if (status) qs.set("status", status);
  const q = useApi(`/partner-network/candidates?${qs.toString()}`);
  const refresh = async () => {
    setBusy(true); setNote("");
    try { const r = await apiPost("/partner-network/discover", { lane, force: true }); setNote(t("src.discovered", { found: r.found ?? 0, contactable: r.contactable ?? 0 })); await q.reload(); }
    catch (e) { setNote(domainError(e, t)); } finally { setBusy(false); }
  };
  const columns = useMemo(() => [
    { key: "name", header: t("src.col.name"), className: "pl-3 max-w-[260px]", headClassName: "pl-3",
      cell: (c) => <span className="flex min-w-0 flex-col"><span className="truncate">{c.name}</span><span className="truncate text-xs text-ink-tertiary">{[c.city, c.domain].filter(Boolean).join(" · ")}</span></span> },
    { key: "business_id", header: t("src.col.bid"), className: "font-mono text-[13px] whitespace-nowrap", cell: (c) => c.business_id || c.wikidata || <Dash /> },
    { key: "country", header: t("src.col.country") },
    { key: "roles", header: t("src.col.roles"), sort: (c) => (c.roles || []).join(), cell: (c) => (c.roles || []).map((r) => tr(t, `src.role.${r}`, r)).join(", ") },
    { key: "score", header: t("src.col.score"), align: "right",
      cell: (c) => <Tip content={(c.score_reasons || []).join("\n")}><span tabIndex={0} className="cursor-help tabular">{num(c.score, locale)}</span></Tip> },
    { key: "contact_email", header: t("src.col.contact"), className: "max-w-[240px]",
      cell: (c) => (c.contact_email ? <span className="block truncate font-mono text-[13px]" title={c.contact_source_url}>{c.contact_email}</span>
        : <span className="block truncate text-xs text-ink-tertiary" title={c.contact_note}>—</span>) },
    { key: "sources", header: t("src.col.source"), sortable: false, cell: (c) => <SourceLinks sources={c.sources || []} /> },
    { key: "status", header: t("src.col.status"), cell: (c) => <StatusBadge size="sm" state={CAND_STATE[c.status] || "expired"} label={tr(t, `src.cand.${c.status}`, c.status)} /> },
    { key: "b2b", header: t("src.col.b2b"), sort: (c) => c.b2b?.basis, className: "pr-3 text-xs text-ink-secondary", headClassName: "pr-3", cell: (c) => (c.b2b?.basis ? t(`src.b2b.${c.b2b.basis.startsWith("business") ? "business_id" : c.b2b.basis.startsWith("company") ? "domain" : "other"}`) : <Dash />) },
  ], [t, locale]);
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <NativeSelect className="w-full sm:w-56" aria-label={t("src.col.lane")} value={lane} placeholder={t("src.lane_all")} onChange={setLane}
          options={lanes.map((l) => ({ value: l, label: laneLabel(l, t) }))} />
        <NativeSelect className="w-full sm:w-56" aria-label={t("src.col.status")} value={status} placeholder={t("src.status_all")} onChange={setStatus}
          options={Object.keys(CAND_STATE).map((s) => ({ value: s, label: t(`src.cand.${s}`) }))} />
        {canRun && lane && <Button variant="outline" className="sm:ml-auto" disabled={busy} onClick={refresh}>{t(busy ? "src.running" : "src.discover")}</Button>}
      </div>
      {note && <p role="status" className="mb-2 text-sm">{note}</p>}
      <Loadable q={q}>{(d) => !d.items.length ? <EmptyState title={t("src.empty.candidates")} /> : narrow ? (
        <ul className="divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
          {d.items.slice(0, 60).map((c) => (
            <MobileRow key={c.id} primary={c.name} status={<StatusBadge size="sm" state={CAND_STATE[c.status] || "expired"} label={tr(t, `src.cand.${c.status}`, c.status)} />}
              secondary={[c.business_id, c.country, c.contact_email].filter(Boolean).join(" · ")} right={num(c.score, locale)} rightSub={t("src.col.score")} />
          ))}
        </ul>
      ) : (
        <>
          <div className="overflow-x-auto rounded-xl border border-[color:var(--border-subtle)]">
            <DataTable columns={columns} rows={d.items} dense pageSize={25} pageLabel="src.page" className="min-w-[1100px]" defaultSort={{ key: "score", dir: "desc" }} />
          </div>
          <p className="mt-2 text-xs text-ink-tertiary">{num(d.items.length, locale)} / {num(d.total, locale)}</p>
        </>
      )}</Loadable>
    </div>
  );
}

function MessageLog({ onOpen }) {
  const { t, locale } = useT();
  const narrow = useIsNarrow();
  const q = useApi("/partner-network/messages?limit=500");
  const columns = [
    { key: "at", header: t("src.col.time"), className: "pl-3 whitespace-nowrap tabular", headClassName: "pl-3", cell: (m) => stamp(m.at, locale) },
    { key: "direction", header: t("src.col.dir"), cell: (m) => <span className="inline-flex items-center gap-1">{m.direction === "in" ? <ArrowDownLeft className="size-3.5" aria-hidden /> : <ArrowUpRight className="size-3.5" aria-hidden />}{t(m.direction === "in" ? "src.in" : "src.out")}</span> },
    { key: "kind", header: t("src.col.kind"), cell: (m) => (m.direction === "in" ? t("src.kind.reply") : tr(t, `src.kind.${m.kind}`, m.kind)) },
    { key: "ref", header: t("src.col.ref"), className: "font-mono text-[13px]", cell: (m) => m.ref || <Dash /> },
    { key: "to", header: t("src.col.to"), className: "max-w-[320px]", sort: (m) => (m.direction === "in" ? m.from : m.to),
      cell: (m) => <span className="flex min-w-0 flex-col"><span className="truncate font-mono text-[13px]">{m.direction === "in" ? m.from : m.to}</span>
        {m.sandbox && m.to_original && <span className="truncate text-xs text-ink-tertiary">{t("src.sandbox_flag")} · {t("src.real", { email: m.to_original })}</span>}</span> },
    { key: "status", header: t("src.col.status"), className: "pr-3", headClassName: "pr-3",
      cell: (m) => (m.status ? <StatusBadge size="sm" state={MSG_STATE[m.status] || "expired"} label={tr(t, `src.msg.${m.status}`, m.status)} /> : <Dash />) },
  ];
  return <Loadable q={q}>{(d) => !d.items.length ? <EmptyState title={t("src.empty.log")} /> : narrow ? (
    <ul className="divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
      {d.items.slice(0, 80).map((m) => (
        <MobileRow key={m.id} onClick={m.outreach_id ? () => onOpen(m.outreach_id) : undefined}
          primary={`${t(m.direction === "in" ? "src.in" : "src.out")} · ${m.direction === "in" ? t("src.kind.reply") : tr(t, `src.kind.${m.kind}`, m.kind)}`}
          status={m.status && <StatusBadge size="sm" state={MSG_STATE[m.status] || "expired"} label={tr(t, `src.msg.${m.status}`, m.status)} />}
          secondary={`${m.ref || ""} · ${m.direction === "in" ? m.from : m.to}`} right={stamp(m.at, locale)} />
      ))}
    </ul>
  ) : (
    <>
      <div className="overflow-x-auto rounded-xl border border-[color:var(--border-subtle)]">
        <DataTable columns={columns} rows={d.items} dense pageSize={50} pageLabel="src.page" className="min-w-[900px]" defaultSort={{ key: "at", dir: "desc" }}
          onRowClick={(m) => m.outreach_id && onOpen(m.outreach_id)} />
      </div>
      <p className="mt-2 text-xs text-ink-tertiary">{num(d.items.length, locale)} / {num(d.total, locale)}</p>
    </>
  )}</Loadable>;
}

function OptOuts({ canWrite, onChanged }) {
  const { t, locale } = useT();
  const q = useApi("/partner-network/optouts");
  const [email, setEmail] = useState("");
  const [domain, setDomain] = useState(false);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const add = async (e) => {
    e.preventDefault(); setBusy(true); setMsg("");
    try { await apiPost("/partner-network/optouts", { email, scope: domain ? "domain" : "address" }); setEmail(""); setDomain(false); setMsg(t("src.optout_saved")); await q.reload(); onChanged?.(); }
    catch (err) { setMsg(domainError(err, t)); } finally { setBusy(false); }
  };
  const columns = [
    { key: "email", header: t("src.col.email"), className: "pl-3 font-mono text-[13px]", headClassName: "pl-3" },
    { key: "scope", header: t("src.col.scope"), cell: (o) => t(`src.scope.${o.scope === "domain" ? "domain" : "address"}`) },
    { key: "source", header: t("src.col.via"), cell: (o) => (o.source?.startsWith("reply") ? t("src.kind.reply") : tr(t, `src.via.${o.source}`, o.source)) },
    { key: "at", header: t("src.col.added"), className: "pr-3 whitespace-nowrap", headClassName: "pr-3", cell: (o) => stamp(o.at, locale) },
  ];
  return (
    <div>
      {canWrite && (
        <form onSubmit={add} className="mb-4 flex flex-wrap items-end gap-3">
          <label className="grid gap-1 text-sm">{t("src.optout_email")}<Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className="w-72" autoComplete="off" /></label>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={domain} onChange={(e) => setDomain(e.target.checked)} />{t("src.optout_domain")}</label>
          <Button type="submit" variant="outline" disabled={busy || !email}>{t("src.optout_add")}</Button>
          {msg && <p role="status" className="w-full text-sm">{msg}</p>}
        </form>
      )}
      <Loadable q={q}>{(d) => !d.items.length ? <EmptyState title={t("src.empty.optouts")} /> : (
        <div className="overflow-x-auto rounded-xl border border-[color:var(--border-subtle)]">
          <DataTable columns={columns} rows={d.items} rowKey="email" dense pageSize={50} pageLabel="src.page" defaultSort={{ key: "at", dir: "desc" }} />
        </div>
      )}</Loadable>
    </div>
  );
}
