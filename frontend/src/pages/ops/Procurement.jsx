// Procurement tabs of the Hankinta page: carrier allocations (MQC / cap), overseas agents (agreements, agent rate cards)
// and partner scorecards with the carrier ranking other agents use. API: /api/procurement/*.
import React, { useMemo, useState } from "react";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { KV, Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { EmptyState, Loadable } from "@/components/app/states";
import { StatusBadge } from "@/components/app/StatusBadge";
import { Dash, Field, MobileRow, NativeSelect, Tip, useIsNarrow } from "@/features/quotes/ops/common";
import { domainError } from "@/features/domainErrors";
import { apiPatch, apiPost, useApi } from "@/lib/api";
import { money, num } from "@/lib/format";
import { useT } from "@/lib/i18n";

const MODES = ["road", "sea", "air", "rail"];
const UNITS = ["TEU", "FEU", "shipments", "loads", "kg"];
const PERIODS = ["week", "month", "quarter"];
const SERVICES = ["pickup", "export_customs", "import_customs", "delivery", "warehousing", "consolidation"];
const BASES = ["PER_SHIPMENT", "PER_KG", "PER_CBM", "PER_CONTAINER"];
const SERVICES_BY_MODE = { sea: ["FCL", "LCL", "RORO"], road: ["ROAD_FTL", "ROAD_LTL"], air: ["AIR"], rail: ["RAIL_FCL", "RAIL_WAGON"] };
const AGREEMENT_STATE = { draft: "in-progress", sent: "submitted", signed: "success", expired: "expired", terminated: "expired" };
const GRADE_STATE = { A: "success", B: "scheduled", C: "pending", D: "failed" };
const tr = (t, key, fallback) => { const s = t(key); return s === key ? fallback : s; };
const today = () => new Date().toISOString().slice(0, 10);
const plusDays = (n) => new Date(Date.now() + n * 86400000).toISOString().slice(0, 10);

function day(iso, locale) {
  if (!iso) return "—";
  const d = new Date(`${iso.slice(0, 10)}T12:00:00Z`);
  return d.toLocaleDateString(locale === "fi" ? "fi-FI" : "en-GB", { day: "2-digit", month: "2-digit", year: "numeric" });
}

/** sea:CN>FI / sea:CNSHA>FIHEL -> "Meri CN → FI" */
export function laneText(lane, t) {
  const m = /^(\w+):([A-Z0-9]{2,5})>([A-Z0-9]{2,5})$/.exec(lane || "");
  return m ? `${t(`enum.mode.${m[1]}`)} ${m[2]} → ${m[3]}` : (lane || "—");
}

function Table({ columns, rows, className, defaultSort, onRowClick, rowKey }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-[color:var(--border-subtle)]">
      <DataTable columns={columns} rows={rows} dense pageSize={50} pageLabel="prc.page" className={className}
        defaultSort={defaultSort} onRowClick={onRowClick} rowKey={rowKey} />
    </div>
  );
}

function MobileList({ children }) {
  return <ul className="divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">{children}</ul>;
}

function Wide({ open, onClose, title, description, children }) {
  return (
    <Sheet open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-y-auto p-0 sm:max-w-[720px]">
        <SheetHeader className="border-b border-[color:var(--border-subtle)] px-5 py-4 text-left">
          <SheetTitle className="text-md font-medium">{title}</SheetTitle>
          <SheetDescription className="flex flex-wrap items-center gap-2 text-xs text-ink-tertiary">{description}</SheetDescription>
        </SheetHeader>
        <div className="px-5 py-4">{children}</div>
      </SheetContent>
    </Sheet>
  );
}

// ------------------------------------------------------------------------------------------------ allocations
function Pace({ s }) {
  const { t } = useT();
  if (!s) return <Dash />;
  if (s.full) return <StatusBadge size="sm" state="failed" label={t("prc.alloc.full")} />;
  if (!s.active) return <StatusBadge size="sm" state="expired" label={t("prc.alloc.inactive")} />;
  return <StatusBadge size="sm" state={s.behind_pace ? "pending" : "success"} label={t(s.behind_pace ? "prc.alloc.behind" : "prc.alloc.on_pace")} />;
}

export function Allocations({ canWrite }) {
  const { t, locale } = useT();
  const narrow = useIsNarrow();
  const q = useApi("/procurement/allocations");
  const [open, setOpen] = useState(null);
  const [creating, setCreating] = useState(false);
  const columns = useMemo(() => [
    { key: "carrier_name", header: t("prc.col.carrier"), className: "pl-3 max-w-[220px]", headClassName: "pl-3",
      cell: (a) => <span className="block truncate">{a.carrier_name}</span> },
    { key: "lane", header: t("prc.col.lane"), cell: (a) => laneText(a.lane, t) },
    { key: "committed_qty", header: t("prc.col.mqc"), align: "right",
      cell: (a) => <span className="tabular">{num(a.committed_qty, locale)} {tr(t, `prc.unit.${a.unit}`, a.unit)} / {t(`prc.period.${a.period}`)}</span> },
    { key: "used", header: t("prc.col.used"), align: "right", sort: (a) => a.status_now?.used,
      cell: (a) => <span className="tabular">{num(a.status_now?.used, locale)} / {num(a.status_now?.committed, locale)}</span> },
    { key: "pace", header: t("prc.col.pace"), sort: (a) => a.status_now?.pace_pct ?? -1, cell: (a) => <Pace s={a.status_now} /> },
    { key: "max_qty", header: t("prc.col.cap"), align: "right", cell: (a) => (a.max_qty == null ? <Dash /> : <span className="tabular">{num(a.status_now?.capacity_left, locale)} / {num(a.max_qty, locale)}</span>) },
    { key: "exposure", header: t("prc.col.exposure"), align: "right", sort: (a) => a.status_now?.exposure_cents || 0,
      cell: (a) => (a.shortfall_cents_per_unit ? <Tip content={t("prc.alloc.exposure_tip", { v: money(a.shortfall_cents_per_unit, locale) })}>
        <span tabIndex={0} className="cursor-help tabular">{money(a.status_now?.exposure_cents || 0, locale)}</span></Tip> : <Dash />) },
    { key: "valid_to", header: t("prc.col.valid"), className: "whitespace-nowrap tabular", cell: (a) => `${day(a.valid_from, locale)} – ${day(a.valid_to, locale)}` },
    { key: "status", header: t("prc.col.status"), className: "pr-3", headClassName: "pr-3",
      cell: (a) => <StatusBadge size="sm" state={a.status === "active" ? "success" : "expired"} label={t(`prc.alloc.status.${a.status}`)} /> },
  ], [t, locale]);
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <p className="max-w-[75ch] text-sm text-ink-secondary">{t("prc.alloc.lead")}</p>
        {canWrite && <Button variant="outline" className="sm:ml-auto" onClick={() => setCreating(true)}><Plus />{t("prc.alloc.new")}</Button>}
      </div>
      <Loadable q={q}>{(d) => !d.items.length ? <EmptyState title={t("prc.alloc.empty")} body={t("prc.alloc.empty_hint")} /> : narrow ? (
        <MobileList>{d.items.map((a) => (
          <MobileRow key={a.id} onClick={() => setOpen(a.id)} primary={a.carrier_name} status={<Pace s={a.status_now} />}
            secondary={`${laneText(a.lane, t)} · ${num(a.committed_qty, locale)} ${tr(t, `prc.unit.${a.unit}`, a.unit)} / ${t(`prc.period.${a.period}`)}`}
            right={`${num(a.status_now?.used, locale)} / ${num(a.status_now?.committed, locale)}`} rightSub={t("prc.col.used")} />
        ))}</MobileList>
      ) : <Table columns={columns} rows={d.items} className="min-w-[1080px]" onRowClick={(a) => setOpen(a.id)} defaultSort={{ key: "valid_to", dir: "desc" }} />}</Loadable>
      <AllocationSheet id={open} onClose={() => setOpen(null)} canWrite={canWrite} onChanged={q.reload} />
      <AllocationDialog open={creating} onClose={() => setCreating(false)} onSaved={(row) => { setCreating(false); q.reload(); setOpen(row.id); }} />
    </div>
  );
}

function AllocationSheet({ id, onClose, canWrite, onChanged }) {
  const { t, locale } = useT();
  const q = useApi(id ? `/procurement/allocations/${id}` : null);
  const [msg, setMsg] = useState("");
  const setStatus = async (status) => {
    setMsg("");
    try { await apiPatch(`/procurement/allocations/${id}`, { status }); await q.reload(); onChanged(); }
    catch (e) { setMsg(domainError(e, t)); }
  };
  const a = q.data;
  const s = a?.status_now;
  return (
    <Wide open={!!id} onClose={onClose} title={a ? `${a.carrier_name} · ${laneText(a.lane, t)}` : t("prc.tab.allocations")}
      description={a && <><Pace s={s} /><span>{s?.period_key}</span></>}>
      <Loadable q={q}>{() => <>
        <KV items={[
          { label: t("prc.col.mqc"), value: `${num(a.committed_qty, locale)} ${tr(t, `prc.unit.${a.unit}`, a.unit)} / ${t(`prc.period.${a.period}`)}` },
          { label: t("prc.col.used"), value: `${num(s.used, locale)} / ${num(s.committed, locale)}`, hint: t("prc.alloc.expected", { n: num(s.expected_by_now, locale) }) },
          { label: t("prc.col.cap"), value: a.max_qty == null ? null : `${num(s.capacity_left, locale)} / ${num(a.max_qty, locale)}` },
          { label: t("prc.alloc.forecast"), value: `${num(s.shortfall_forecast, locale)} ${tr(t, `prc.unit.${a.unit}`, a.unit)}`,
            hint: a.shortfall_cents_per_unit ? money(s.exposure_cents, locale) : null },
          { label: t("prc.alloc.penalty"), value: a.shortfall_cents_per_unit ? `${money(a.shortfall_cents_per_unit, locale)} / ${tr(t, `prc.unit.${a.unit}`, a.unit)}` : t("prc.alloc.soft") },
          { label: t("prc.col.valid"), value: `${day(a.valid_from, locale)} – ${day(a.valid_to, locale)}`, hint: `${day(s.period_start, locale)} – ${day(s.period_end, locale)}` },
          { label: t("prc.alloc.notes"), value: a.notes },
        ]} />
        <p className="mt-3 text-xs text-ink-tertiary">{t("prc.alloc.how")}</p>
        <Section title={t("prc.alloc.usage", { n: a.usage.length })} className="mt-5">
          {!a.usage.length ? <p className="text-sm text-ink-tertiary">{t("prc.alloc.no_usage")}</p> : (
            <ul className="divide-y divide-[color:var(--border-subtle)] text-sm">
              {a.usage.map((u) => (
                <li key={u.shipment_id} className="flex items-center justify-between gap-3 py-2">
                  <span className="font-mono text-[13px]">{u.shipment_ref || u.shipment_id.slice(0, 8)}</span>
                  <span className="text-xs text-ink-tertiary">{u.period_key} · {day(u.day, locale)}{u.released ? ` · ${t("prc.alloc.released")}` : ""}</span>
                  <span className="tabular">{num(u.qty, locale)} {tr(t, `prc.unit.${a.unit}`, a.unit)}</span>
                </li>
              ))}
            </ul>
          )}
        </Section>
        {canWrite && <div className="mt-4 flex flex-wrap gap-2">
          {a.status === "active" && <Button variant="outline" onClick={() => setStatus("paused")}>{t("prc.alloc.pause")}</Button>}
          {a.status === "paused" && <Button variant="outline" onClick={() => setStatus("active")}>{t("prc.alloc.resume")}</Button>}
          {a.status !== "ended" && <Button variant="outline" className="text-danger-fg" onClick={() => setStatus("ended")}>{t("prc.alloc.end")}</Button>}
        </div>}
        {msg && <p role="alert" className="mt-2 text-sm text-destructive">{msg}</p>}
      </>}</Loadable>
    </Wide>
  );
}

function AllocationDialog({ open, onClose, onSaved }) {
  const { t } = useT();
  const carriers = useApi(open ? "/carriers" : null);
  const [f, setF] = useState({ mode: "sea", origin: "", destination: "FI", unit: "TEU", committed_qty: "", max_qty: "", period: "month",
    valid_from: today(), valid_to: plusDays(365), penalty: "", carrier_id: null, notes: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v?.target ? v.target.value : v }));
  const save = async (e) => {
    e.preventDefault(); setBusy(true); setErr("");
    try {
      const row = await apiPost("/procurement/allocations", {
        carrier_id: f.carrier_id, lane: `${f.mode}:${f.origin.trim().toUpperCase()}>${f.destination.trim().toUpperCase()}`, unit: f.unit,
        committed_qty: Number(f.committed_qty), max_qty: f.max_qty === "" ? null : Number(f.max_qty), period: f.period,
        valid_from: f.valid_from, valid_to: f.valid_to, shortfall_cents_per_unit: Math.round(Number(f.penalty || 0) * 100), notes: f.notes || null });
      onSaved(row);
    } catch (e2) { setErr(domainError(e2, t)); } finally { setBusy(false); }
  };
  const list = (carriers.data?.items || []).filter((c) => (c.modes || []).includes(f.mode) && c.org_type !== "agent");
  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader><DialogTitle>{t("prc.alloc.new")}</DialogTitle><DialogDescription>{t("prc.alloc.new_hint")}</DialogDescription></DialogHeader>
        <form onSubmit={save} className="grid gap-3 sm:grid-cols-2">
          <Field label={t("prc.col.mode")}><NativeSelect aria-label={t("prc.col.mode")} value={f.mode} onChange={set("mode")} options={MODES.map((m) => ({ value: m, label: t(`enum.mode.${m}`) }))} /></Field>
          <Field label={t("prc.col.carrier")}><NativeSelect aria-label={t("prc.col.carrier")} value={f.carrier_id} onChange={set("carrier_id")} placeholder={t("prc.choose")} required
            options={list.map((c) => ({ value: c.id, label: c.name }))} /></Field>
          <Field label={t("prc.alloc.origin")} hint={t("prc.alloc.place_hint")}><Input aria-label={t("prc.alloc.origin")} required value={f.origin} onChange={set("origin")} maxLength={5} placeholder="CN" /></Field>
          <Field label={t("prc.alloc.destination")}><Input aria-label={t("prc.alloc.destination")} required value={f.destination} onChange={set("destination")} maxLength={5} placeholder="FI" /></Field>
          <Field label={t("prc.col.unit")}><NativeSelect aria-label={t("prc.col.unit")} value={f.unit} onChange={set("unit")} options={UNITS.map((u) => ({ value: u, label: tr(t, `prc.unit.${u}`, u) }))} /></Field>
          <Field label={t("prc.col.period")}><NativeSelect aria-label={t("prc.col.period")} value={f.period} onChange={set("period")} options={PERIODS.map((p) => ({ value: p, label: t(`prc.period.${p}`) }))} /></Field>
          <Field label={t("prc.col.mqc")}><Input aria-label={t("prc.col.mqc")} required type="number" min="0" step="any" value={f.committed_qty} onChange={set("committed_qty")} /></Field>
          <Field label={t("prc.col.cap")} hint={t("prc.optional")}><Input aria-label={t("prc.col.cap")} type="number" min="0" step="any" value={f.max_qty} onChange={set("max_qty")} /></Field>
          <Field label={t("prc.alloc.valid_from")}><Input aria-label={t("prc.alloc.valid_from")} required type="date" value={f.valid_from} onChange={set("valid_from")} /></Field>
          <Field label={t("prc.alloc.valid_to")}><Input aria-label={t("prc.alloc.valid_to")} required type="date" value={f.valid_to} onChange={set("valid_to")} /></Field>
          <Field label={t("prc.alloc.penalty_eur")} hint={t("prc.alloc.penalty_hint")}><Input aria-label={t("prc.alloc.penalty_eur")} type="number" min="0" step="0.01" value={f.penalty} onChange={set("penalty")} /></Field>
          <Field label={t("prc.alloc.notes")}><Input aria-label={t("prc.alloc.notes")} value={f.notes} onChange={set("notes")} /></Field>
          {err && <p role="alert" className="text-sm text-destructive sm:col-span-2">{err}</p>}
          <DialogFooter className="sm:col-span-2">
            <Button type="button" variant="ghost" onClick={onClose}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={busy || !f.carrier_id}>{t("prc.save")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ------------------------------------------------------------------------------------------------ agents
function AgreementBadge({ a }) {
  const { t } = useT();
  if (!a) return <StatusBadge size="sm" state="expired" label={t("prc.agr.none")} />;
  return <StatusBadge size="sm" state={AGREEMENT_STATE[a.state] || "expired"} label={t(`prc.agr.${a.state}`)} />;
}

export function Agents({ canWrite }) {
  const { t, locale } = useT();
  const narrow = useIsNarrow();
  const q = useApi("/procurement/agents");
  const [open, setOpen] = useState(null);
  const [creating, setCreating] = useState(false);
  const latest = (ag) => ag.agreements?.find((a) => a.live) || ag.agreements?.[0];
  const columns = useMemo(() => [
    { key: "name", header: t("prc.col.agent"), className: "pl-3 max-w-[260px]", headClassName: "pl-3",
      cell: (a) => <span className="flex min-w-0 flex-col"><span className="truncate">{a.name}</span><span className="truncate text-xs text-ink-tertiary">{a.contact_email || a.website || ""}</span></span> },
    { key: "countries", header: t("prc.col.countries"), sort: (a) => a.agent?.countries?.join(), cell: (a) => (a.agent?.countries || []).join(", ") },
    { key: "roles", header: t("prc.col.roles"), sort: (a) => a.agent?.roles?.join(), cell: (a) => (a.agent?.roles || []).map((r) => t(`prc.role.${r}`)).join(", ") },
    { key: "modes", header: t("prc.col.modes"), sortable: false, cell: (a) => (a.agent?.modes?.length ? a.agent.modes.map((m) => t(`enum.mode.${m}`)).join(", ") : t("prc.all_modes")) },
    { key: "agreement", header: t("prc.col.agreement"), sort: (a) => latest(a)?.state || "", cell: (a) => <AgreementBadge a={latest(a)} /> },
    { key: "rate_cards", header: t("prc.col.rate_cards"), align: "right", sort: (a) => a.rate_cards?.length || 0, cell: (a) => <span className="tabular">{num(a.rate_cards?.length || 0, locale)}</span> },
    { key: "source", header: t("prc.col.source"), className: "pr-3 text-xs text-ink-secondary", headClassName: "pr-3",
      cell: (a) => tr(t, `prc.agent_source.${a.source}`, a.source) },
  ], [t, locale]);
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <p className="max-w-[75ch] text-sm text-ink-secondary">{t("prc.agent.lead")}</p>
        {canWrite && <Button variant="outline" className="sm:ml-auto" onClick={() => setCreating(true)}><Plus />{t("prc.agent.new")}</Button>}
      </div>
      <Loadable q={q}>{(d) => !d.items.length ? <EmptyState title={t("prc.agent.empty")} body={t("prc.agent.empty_hint")} /> : narrow ? (
        <MobileList>{d.items.map((a) => (
          <MobileRow key={a.id} onClick={() => setOpen(a.id)} primary={a.name} status={<AgreementBadge a={latest(a)} />}
            secondary={`${(a.agent?.countries || []).join(", ")} · ${(a.agent?.roles || []).map((r) => t(`prc.role.${r}`)).join(", ")}`}
            right={num(a.rate_cards?.length || 0, locale)} rightSub={t("prc.col.rate_cards")} />
        ))}</MobileList>
      ) : <Table columns={columns} rows={d.items} className="min-w-[980px]" onRowClick={(a) => setOpen(a.id)} defaultSort={{ key: "name", dir: "asc" }} />}</Loadable>
      <AgentSheet id={open} onClose={() => setOpen(null)} canWrite={canWrite} onChanged={q.reload} />
      <AgentDialog open={creating} onClose={() => setCreating(false)} onSaved={(row) => { setCreating(false); q.reload(); setOpen(row.id); }} />
    </div>
  );
}

function AgentDialog({ open, onClose, onSaved }) {
  const { t } = useT();
  const [f, setF] = useState({ name: "", country: "", countries: "", roles: ["origin", "destination"], modes: [], services: [], contact_email: "", website: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));
  const toggle = (k, v) => setF((x) => ({ ...x, [k]: x[k].includes(v) ? x[k].filter((y) => y !== v) : [...x[k], v] }));
  const save = async (e) => {
    e.preventDefault(); setBusy(true); setErr("");
    try {
      const countries = (f.countries || f.country).split(/[\s,;]+/).filter(Boolean);
      onSaved(await apiPost("/procurement/agents", { ...f, country: f.country.trim().toUpperCase(), countries }));
    } catch (e2) { setErr(domainError(e2, t)); } finally { setBusy(false); }
  };
  const Checks = ({ k, values, label }) => (
    <fieldset className="sm:col-span-2"><legend className="mb-1 text-xs font-medium text-ink-secondary">{t(label)}</legend>
      <div className="flex flex-wrap gap-x-4 gap-y-1">{values.map((v) => (
        <label key={v} className="flex items-center gap-1.5 text-sm"><input type="checkbox" checked={f[k].includes(v)} onChange={() => toggle(k, v)} />
          {k === "modes" ? t(`enum.mode.${v}`) : t(`prc.${k === "roles" ? "role" : "service"}.${v}`)}</label>))}</div>
    </fieldset>
  );
  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader><DialogTitle>{t("prc.agent.new")}</DialogTitle><DialogDescription>{t("prc.agent.new_hint")}</DialogDescription></DialogHeader>
        <form onSubmit={save} className="grid gap-3 sm:grid-cols-2">
          <Field label={t("prc.col.agent")} className="sm:col-span-2"><Input aria-label={t("prc.col.agent")} required value={f.name} onChange={set("name")} /></Field>
          <Field label={t("prc.agent.home")}><Input aria-label={t("prc.agent.home")} required value={f.country} onChange={set("country")} maxLength={2} placeholder="DE" /></Field>
          <Field label={t("prc.col.countries")} hint={t("prc.agent.countries_hint")}><Input aria-label={t("prc.col.countries")} value={f.countries} onChange={set("countries")} placeholder="DE, PL" /></Field>
          <Field label={t("prc.agent.email")}><Input aria-label={t("prc.agent.email")} type="email" value={f.contact_email} onChange={set("contact_email")} /></Field>
          <Field label={t("prc.agent.website")}><Input aria-label={t("prc.agent.website")} type="url" value={f.website} onChange={set("website")} placeholder="https://" /></Field>
          <Checks k="roles" values={["origin", "destination"]} label="prc.col.roles" />
          <Checks k="modes" values={MODES} label="prc.col.modes" />
          <Checks k="services" values={SERVICES} label="prc.col.services" />
          {err && <p role="alert" className="text-sm text-destructive sm:col-span-2">{err}</p>}
          <DialogFooter className="sm:col-span-2">
            <Button type="button" variant="ghost" onClick={onClose}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={busy}>{t("prc.save")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function AgentSheet({ id, onClose, canWrite, onChanged }) {
  const { t, locale } = useT();
  const q = useApi(id ? `/procurement/agents/${id}` : null);
  const [msg, setMsg] = useState("");
  const [signer, setSigner] = useState("");
  const [agr, setAgr] = useState({ valid_from: today(), valid_to: plusDays(365), payment_days: "30", profit_share_pct: "50" });
  const [card, setCard] = useState({ role: "origin", country: "", mode: "sea", valid_from: today(), valid_to: plusDays(180),
    charges: [{ code: "HANDLING", label: "Handling", label_fi: "Käsittely", basis: "PER_SHIPMENT", amount: "" }] });
  const run = async (fn) => { setMsg(""); try { await fn(); await q.reload(); onChanged(); } catch (e) { setMsg(domainError(e, t)); } };
  const a = q.data;
  const live = a?.agreements?.find((x) => x.live);
  const setCh = (i, k, v) => setCard((c) => ({ ...c, charges: c.charges.map((ch, j) => (j === i ? { ...ch, [k]: v } : ch)) }));
  return (
    <Wide open={!!id} onClose={onClose} title={a?.name || t("prc.tab.agents")}
      description={a && <><AgreementBadge a={live || a.agreements?.[0]} /><span>{(a.agent?.countries || []).join(", ")}</span></>}>
      <Loadable q={q}>{() => <>
        <KV items={[
          { label: t("prc.col.roles"), value: (a.agent?.roles || []).map((r) => t(`prc.role.${r}`)).join(", ") },
          { label: t("prc.col.services"), value: (a.agent?.services || []).map((s) => t(`prc.service.${s}`)).join(", ") },
          { label: t("prc.col.modes"), value: a.agent?.modes?.length ? a.agent.modes.map((m) => t(`enum.mode.${m}`)).join(", ") : t("prc.all_modes") },
          { label: t("prc.agent.email"), value: a.contact_email, mono: true },
          { label: t("prc.agent.website"), value: a.website && <a className="break-all text-ink-accent hover:underline" href={a.website} target="_blank" rel="noreferrer">{a.website}</a> },
          { label: t("prc.col.source"), value: (a.sources || []).length ? (a.sources || []).map((s) => <a key={s.url} className="mr-2 text-ink-accent hover:underline" href={s.url} target="_blank" rel="noreferrer">{tr(t, `src.source.${s.source}`, s.source)}</a>) : tr(t, `prc.agent_source.${a.source}`, a.source) },
        ]} />
        <Section title={t("prc.agr.title")} className="mt-5">
          {!a.agreements.length && <p className="text-sm text-ink-tertiary">{t("prc.agr.empty")}</p>}
          <ul className="divide-y divide-[color:var(--border-subtle)]">
            {a.agreements.map((g) => (
              <li key={g.id} className="py-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <AgreementBadge a={g} />
                  <span className="tabular">{day(g.valid_from, locale)} – {day(g.valid_to, locale)}</span>
                  <span className="text-xs text-ink-tertiary">{t("prc.agr.terms", { days: g.terms.payment_days, share: num(g.terms.profit_share_pct, locale) })}</span>
                </div>
                {g.signed_by && <p className="mt-1 text-xs text-ink-tertiary">{t("prc.agr.signed_line", { who: g.signed_by, at: day(g.signed_at, locale), by: g.countersigned_by?.name || "—" })}</p>}
                {canWrite && ["draft", "sent"].includes(g.status) && (
                  <div className="mt-2 flex flex-wrap items-end gap-2">
                    {g.status === "draft" && <Button size="sm" variant="outline" onClick={() => run(() => apiPost(`/procurement/agreements/${g.id}/sent`, {}))}>{t("prc.agr.mark_sent")}</Button>}
                    <Input className="h-8 w-60" value={signer} onChange={(e) => setSigner(e.target.value)} placeholder={t("prc.agr.signer")} aria-label={t("prc.agr.signer")} />
                    <Button size="sm" disabled={signer.trim().length < 3} onClick={() => run(() => apiPost(`/procurement/agreements/${g.id}/signed`, { signed_by: signer }))}>{t("prc.agr.record_signed")}</Button>
                  </div>
                )}
                {canWrite && g.status === "signed" && g.state !== "expired" && (
                  <Button size="sm" variant="outline" className="mt-2 text-danger-fg" onClick={() => run(() => apiPost(`/procurement/agreements/${g.id}/terminated`, {}))}>{t("prc.agr.terminate")}</Button>
                )}
              </li>
            ))}
          </ul>
          {canWrite && (
            <form className="mt-3 grid gap-2 sm:grid-cols-4" onSubmit={(e) => { e.preventDefault(); run(() => apiPost(`/procurement/agents/${id}/agreements`, {
              valid_from: agr.valid_from, valid_to: agr.valid_to, terms: { payment_days: Number(agr.payment_days), profit_share_pct: Number(agr.profit_share_pct) } })); }}>
              <Field label={t("prc.alloc.valid_from")}><Input aria-label={t("prc.alloc.valid_from")} type="date" value={agr.valid_from} onChange={(e) => setAgr({ ...agr, valid_from: e.target.value })} /></Field>
              <Field label={t("prc.alloc.valid_to")}><Input aria-label={t("prc.alloc.valid_to")} type="date" value={agr.valid_to} onChange={(e) => setAgr({ ...agr, valid_to: e.target.value })} /></Field>
              <Field label={t("prc.agr.payment_days")}><Input aria-label={t("prc.agr.payment_days")} type="number" min="0" max="120" value={agr.payment_days} onChange={(e) => setAgr({ ...agr, payment_days: e.target.value })} /></Field>
              <Field label={t("prc.agr.profit_share")}><Input aria-label={t("prc.agr.profit_share")} type="number" min="0" max="50" value={agr.profit_share_pct} onChange={(e) => setAgr({ ...agr, profit_share_pct: e.target.value })} /></Field>
              <Button type="submit" variant="outline" className="sm:col-span-4 sm:justify-self-start"><Plus />{t("prc.agr.new")}</Button>
            </form>
          )}
          <p className="mt-2 text-xs text-ink-tertiary">{t("prc.agr.no_esign")}</p>
        </Section>
        <Section title={t("prc.agent.rates", { n: a.rate_cards.length })}>
          {!a.rate_cards.length && <p className="text-sm text-ink-tertiary">{t("prc.agent.no_rates")}</p>}
          <ul className="divide-y divide-[color:var(--border-subtle)]">
            {a.rate_cards.map((c) => (
              <li key={c.id} className="py-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{t(`prc.role.${c.agent_role}`)} {c.agent_country}</span>
                  <span className="text-ink-tertiary">{t(`enum.mode.${c.mode}`)}</span>
                  <span className="tabular text-xs text-ink-tertiary">{day(c.valid_from, locale)} – {day(c.valid_to, locale)}</span>
                </div>
                <ul className="mt-1 text-xs text-ink-secondary">
                  {(c.charges || []).map((ch) => <li key={ch.code} className="flex justify-between gap-3"><span>{locale === "fi" ? ch.label_fi : ch.label}</span>
                    <span className="tabular">{money(ch.amount_cents, locale)} {t(`prc.basis.${ch.basis}`)}{ch.min_cents ? ` · min ${money(ch.min_cents, locale)}` : ""}</span></li>)}
                </ul>
              </li>
            ))}
          </ul>
          {canWrite && (
            <form className="mt-3 grid gap-2 sm:grid-cols-5" onSubmit={(e) => { e.preventDefault(); run(() => apiPost(`/procurement/agents/${id}/rate-cards`, {
              role: card.role, country: (card.country || a.agent.countries[0]).toUpperCase(), mode: card.mode, valid_from: card.valid_from, valid_to: card.valid_to,
              charges: card.charges.map((ch) => ({ code: ch.code, label: ch.label, label_fi: ch.label_fi, basis: ch.basis, amount_cents: Math.round(Number(ch.amount) * 100) })) })); }}>
              <Field label={t("prc.col.roles")}><NativeSelect aria-label={t("prc.col.roles")} value={card.role} onChange={(v) => setCard({ ...card, role: v })} options={(a.agent?.roles || []).map((r) => ({ value: r, label: t(`prc.role.${r}`) }))} /></Field>
              <Field label={t("prc.col.country")}><NativeSelect aria-label={t("prc.col.country")} value={card.country || a.agent.countries[0]} onChange={(v) => setCard({ ...card, country: v })} options={(a.agent?.countries || []).map((c) => ({ value: c, label: c }))} /></Field>
              <Field label={t("prc.col.mode")}><NativeSelect aria-label={t("prc.col.mode")} value={card.mode} onChange={(v) => setCard({ ...card, mode: v })} options={MODES.map((m) => ({ value: m, label: t(`enum.mode.${m}`) }))} /></Field>
              <Field label={t("prc.alloc.valid_from")}><Input aria-label={t("prc.alloc.valid_from")} type="date" value={card.valid_from} onChange={(e) => setCard({ ...card, valid_from: e.target.value })} /></Field>
              <Field label={t("prc.alloc.valid_to")}><Input aria-label={t("prc.alloc.valid_to")} type="date" value={card.valid_to} onChange={(e) => setCard({ ...card, valid_to: e.target.value })} /></Field>
              {card.charges.map((ch, i) => (
                <React.Fragment key={i}>
                  <Field label={i === 0 ? t("prc.charge.code") : undefined}><Input value={ch.code} onChange={(e) => setCh(i, "code", e.target.value)} aria-label={t("prc.charge.code")} /></Field>
                  <Field label={i === 0 ? t("prc.charge.label_fi") : undefined}><Input value={ch.label_fi} onChange={(e) => setCh(i, "label_fi", e.target.value)} aria-label={t("prc.charge.label_fi")} /></Field>
                  <Field label={i === 0 ? t("prc.charge.label_en") : undefined}><Input value={ch.label} onChange={(e) => setCh(i, "label", e.target.value)} aria-label={t("prc.charge.label_en")} /></Field>
                  <Field label={i === 0 ? t("prc.charge.basis") : undefined}><NativeSelect value={ch.basis} onChange={(v) => setCh(i, "basis", v)} aria-label={t("prc.charge.basis")} options={BASES.map((b) => ({ value: b, label: t(`prc.basis.${b}`) }))} /></Field>
                  <Field label={i === 0 ? t("prc.charge.amount") : undefined}><Input type="number" min="0" step="0.01" required value={ch.amount} onChange={(e) => setCh(i, "amount", e.target.value)} aria-label={t("prc.charge.amount")} /></Field>
                </React.Fragment>
              ))}
              <div className="flex flex-wrap gap-2 sm:col-span-5">
                <Button type="button" variant="ghost" onClick={() => setCard({ ...card, charges: [...card.charges, { code: "", label: "", label_fi: "", basis: "PER_SHIPMENT", amount: "" }] })}><Plus />{t("prc.charge.add")}</Button>
                <Button type="submit" variant="outline">{t("prc.agent.add_rate")}</Button>
              </div>
            </form>
          )}
        </Section>
        {msg && <p role="alert" className="mt-2 text-sm text-destructive">{msg}</p>}
      </>}</Loadable>
    </Wide>
  );
}

// ------------------------------------------------------------------------------------------------ scorecards
function MetricCell({ m, locale }) {
  const { t } = useT();
  if (!m) return <span className="text-xs text-ink-tertiary">{t("prc.sc.unavailable")}</span>;
  const value = m.value == null ? t("prc.sc.unavailable") : m.key === "response" ? `${num(m.value, locale)} h` : m.key === "cost" ? num(m.value, locale) : `${num(m.value, locale)}${m.key === "claims" ? "" : " %"}`;
  return (
    <Tip content={`${locale === "fi" ? m.note_fi : m.note_en}\n${t("prc.sc.sample", { n: m.sample, min: m.min_sample })}`}>
      <span tabIndex={0} className={`cursor-help tabular ${m.enough ? "" : "text-ink-tertiary"}`}>
        {value}<span className="ml-1 text-xs text-ink-tertiary">({m.sample})</span>
      </span>
    </Tip>
  );
}

function Grade({ row }) {
  const { t, locale } = useT();
  if (!row || row.score == null) return <span className="text-xs text-ink-tertiary">{t("prc.sc.not_enough")}</span>;
  return <span className="inline-flex items-center gap-2"><span className="tabular">{num(row.score, locale)}</span><StatusBadge size="sm" state={GRADE_STATE[row.grade]} label={row.grade} icon={false} /></span>;
}

export function Scorecards() {
  const { t, locale } = useT();
  const narrow = useIsNarrow();
  const [refresh, setRefresh] = useState(0);
  const q = useApi(`/procurement/scorecards${refresh ? `?refresh=1&n=${refresh}` : ""}`);
  const [open, setOpen] = useState(null);
  const metric = (row, key) => row.metrics.find((m) => m.key === key);
  const columns = useMemo(() => [
    { key: "carrier_name", header: t("prc.col.carrier"), className: "pl-3 max-w-[240px]", headClassName: "pl-3",
      cell: (r) => <span className="flex min-w-0 flex-col"><span className="truncate">{r.carrier_name}</span><span className="text-xs text-ink-tertiary">{t(`prc.org.${r.org_type}`)}</span></span> },
    { key: "score", header: t("prc.col.score"), sort: (r) => r.score ?? -1, cell: (r) => <Grade row={r} /> },
    ...["on_time", "cost", "claims", "response", "invoice_accuracy"].map((k) => ({
      key: k, header: t(`prc.sc.${k}`), align: "right", sort: (r) => metric(r, k)?.value ?? -1, cell: (r) => <MetricCell m={metric(r, k)} locale={locale} /> })),
    { key: "coverage_pct", header: t("prc.col.coverage"), align: "right", className: "pr-3", headClassName: "pr-3", cell: (r) => <span className="tabular">{num(r.coverage_pct, locale)} %</span> },
  ], [t, locale]);
  return (
    <div>
      <RankTool />
      <div className="mb-3 mt-6 flex flex-wrap items-center gap-2">
        <p className="max-w-[80ch] text-sm text-ink-secondary">{t("prc.sc.lead")}</p>
        <Button variant="outline" className="sm:ml-auto" onClick={() => setRefresh((n) => n + 1)} disabled={q.refreshing}>{t("prc.sc.recompute")}</Button>
      </div>
      <Loadable q={q}>{(d) => <>
        {d.items.length > 0 && d.items.every((r) => metric(r, "claims")?.sample === 0) &&
          <EmptyState title={t("prc.sc.no_real_shipments")} body={t("prc.sc.no_real_shipments_hint")} />}
        {!d.items.length ? <EmptyState title={t("prc.sc.empty")} /> : narrow ? (
        <MobileList>{d.items.map((r) => (
          <MobileRow key={r.carrier_id} onClick={() => setOpen(r)} primary={r.carrier_name} status={r.grade && <StatusBadge size="sm" state={GRADE_STATE[r.grade]} label={r.grade} icon={false} />}
            secondary={`${t(`prc.org.${r.org_type}`)} · ${t("prc.col.coverage")} ${num(r.coverage_pct, locale)} %`}
            right={r.score == null ? t("prc.sc.not_enough") : num(r.score, locale)} rightSub={t("prc.col.score")} />
        ))}</MobileList>
      ) : <Table columns={columns} rows={d.items} rowKey="carrier_id" className="min-w-[1040px]" onRowClick={setOpen} defaultSort={{ key: "score", dir: "desc" }} />}</>}</Loadable>
      <Wide open={!!open} onClose={() => setOpen(null)} title={open?.carrier_name} description={open && <><Grade row={open} /><span>{t("prc.sc.lookback", { d: open.lookback_days })}</span></>}>
        {open && <ul className="divide-y divide-[color:var(--border-subtle)]">
          {open.metrics.map((m) => (
            <li key={m.key} className="py-3">
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-medium">{locale === "fi" ? m.label_fi : m.label_en}</span>
                <MetricCell m={m} locale={locale} />
              </div>
              <p className="mt-1 text-xs text-ink-secondary">{locale === "fi" ? m.note_fi : m.note_en}</p>
              <p className="mt-0.5 text-xs text-ink-tertiary">{m.enough ? t("prc.sc.in_score", { s: num(m.score, locale) }) : t("prc.sc.left_out", { n: m.sample, min: m.min_sample })} · {m.source}</p>
              {m.key === "cost" && (m.detail.lanes || []).length > 0 && (
                <ul className="mt-1 text-xs text-ink-tertiary">{m.detail.lanes.map((l) => (
                  <li key={l.lane + l.service}>{laneText(l.lane, t)} {l.service}: {money(l.own_cents, locale)} / {t("prc.sc.median")} {money(l.median_cents, locale)} ({t("prc.sc.cards", { n: l.cards_on_lane, ind: l.indicative_on_lane })})</li>))}</ul>
              )}
            </li>
          ))}
        </ul>}
      </Wide>
    </div>
  );
}

function RankTool() {
  const { t, locale } = useT();
  const [f, setF] = useState({ mode: "road", service: "ROAD_FTL", origin: "FI", destination: "SE" });
  const [path, setPath] = useState(null);
  const q = useApi(path);
  const go = (e) => { e.preventDefault(); setPath(`/procurement/rank?${new URLSearchParams({ mode: f.mode, service: f.service, origin: f.origin.toUpperCase(), destination: f.destination.toUpperCase() })}`); };
  return (
    <Section title={t("prc.rank.title")}>
      <p className="mb-2 max-w-[80ch] text-xs text-ink-tertiary">{t("prc.rank.lead")}</p>
      <form onSubmit={go} className="mb-3 flex flex-wrap items-end gap-2">
        <Field label={t("prc.col.mode")}><NativeSelect aria-label={t("prc.col.mode")} className="w-32" value={f.mode} onChange={(v) => setF({ ...f, mode: v, service: SERVICES_BY_MODE[v][0] })} options={MODES.map((m) => ({ value: m, label: t(`enum.mode.${m}`) }))} /></Field>
        <Field label={t("prc.col.service")}><NativeSelect aria-label={t("prc.col.service")} className="w-36" value={f.service} onChange={(v) => setF({ ...f, service: v })} options={SERVICES_BY_MODE[f.mode].map((s) => ({ value: s, label: tr(t, `prc.svc.${s}`, s) }))} /></Field>
        <Field label={t("prc.alloc.origin")}><Input aria-label={t("prc.alloc.origin")} className="w-20" maxLength={2} value={f.origin} onChange={(e) => setF({ ...f, origin: e.target.value })} /></Field>
        <Field label={t("prc.alloc.destination")}><Input aria-label={t("prc.alloc.destination")} className="w-20" maxLength={2} value={f.destination} onChange={(e) => setF({ ...f, destination: e.target.value })} /></Field>
        <Button type="submit" variant="outline">{t("prc.rank.go")}</Button>
      </form>
      {path && <Loadable q={q}>{(d) => !d.items.length ? <p className="text-sm text-ink-tertiary">{t("prc.rank.none")}</p> : (
        <ol className="divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
          {d.items.map((r) => (
            <li key={r.carrier_id} className="flex flex-wrap items-start gap-x-3 gap-y-1 px-3 py-2 text-sm">
              <span className="w-5 tabular text-ink-tertiary">{r.rank}.</span>
              <span className="min-w-0 flex-1">
                <span className="font-medium">{r.name}</span>
                <span className="block text-xs text-ink-tertiary">{r.reasons.map((x) => (locale === "fi" ? x.fi : x.en)).join(" · ")}</span>
              </span>
              <span className="tabular">{money(r.unit_cost_cents, locale)}</span>
              {r.grade ? <StatusBadge size="sm" state={GRADE_STATE[r.grade]} label={r.grade} icon={false} /> : <span className="text-xs text-ink-tertiary">{t("prc.sc.not_enough")}</span>}
            </li>
          ))}
        </ol>
      )}</Loadable>}
    </Section>
  );
}
