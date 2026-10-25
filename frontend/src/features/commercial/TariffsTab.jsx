// Sopimushinnat: customer contract tariffs. Decision: which drafts to activate, which tariffs run out soon.
import React, { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { Clock, Info, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DateInput } from "@/components/ui/DateInput";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { KV, PageHeader } from "@/components/app/layout";
import { EmptyState, Freshness } from "@/components/app/states";
import { DataTable } from "@/components/app/DataTable";
import { Segmented } from "@/components/app/FilterBar";
import { useConfirmChange } from "@/components/app/ConfirmChange";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, daysUntil, num } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { Field, MODES, ModeLabel, MoneyInput, NativeSelect, NumInput, useIsNarrow } from "@/features/quotes/ops/common";
import { Chip, ComBadge, ComLoadable, Eur, NeedsLine, Place, ScrollTable, mutationError, useLabel } from "./common";
import { listOf, tariff as normTariff } from "./shapes";
import { useCustomerOptions } from "./TendersTab";

const UNITS = ["shipment", "container", "teu", "kg", "ldm", "pallet", "cbm"];
const expiringSoon = (r) => r.status === "active" && r.valid_to && daysUntil(r.valid_to) !== null && daysUntil(r.valid_to) <= 30;

export function Precedence() {
  const { t } = useT();
  return <p className="mb-3 inline-flex items-center gap-1.5 text-xs text-ink-tertiary"><Info className="size-3.5" aria-hidden />{t("com.tariff.precedence")}</p>;
}

export default function TariffsTab() {
  const { t, locale } = useT();
  const label = useLabel();
  const { can } = useAuth();
  const canWrite = can("quotes.write");
  const narrow = useIsNarrow();
  const navigate = useNavigate();
  const [status, setStatus] = useState(null);
  const [creating, setCreating] = useState(false);
  const q = useApi("/tariffs", { pollMs: 60000 });
  const all = useMemo(() => listOf(q.data, "tariffs").map(normTariff), [q.data]);
  const rows = useMemo(() => all.filter((r) => !status || r.status === status)
    .sort((a, b) => (a.customer || "").localeCompare(b.customer || "", locale) || String(b.valid_from || "").localeCompare(String(a.valid_from || ""))), [all, status, locale]);
  const drafts = all.filter((r) => r.status === "draft").length;
  const soon = all.filter(expiringSoon).length;
  const counts = all.reduce((a, r) => ({ ...a, [r.status]: (a[r.status] || 0) + 1 }), {});

  const columns = [
    { key: "customer", header: t("field.customer"), sort: (r) => (r.customer || "").toLowerCase(), cell: (r) => (
      <span className="flex min-w-0 max-w-[260px] flex-col">
        <Link to={`/ops/commercial/tariffs/${r.id}`} onClick={(e) => e.stopPropagation()} className="truncate font-medium hover:underline">{r.customer || "—"}</Link>
        <span className="truncate text-xs text-ink-tertiary">{r.name}</span>
      </span>
    ) },
    { key: "status", header: t("field.status"), cell: (r) => <ComBadge group="tariff" value={r.status} /> },
    { key: "valid_to", header: t("com.tariff.validity"), cell: (r) => <Validity r={r} /> },
    { key: "lane_count", header: t("com.tender.lanes"), align: "right", cell: (r) => num(r.lane_count, locale) },
    { key: "source", header: t("com.tariff.from"), sortable: false, hideBelow: "md", cell: (r) => r.tender_id ? <Link className="text-xs text-ink-accent hover:underline" onClick={(e) => e.stopPropagation()} to={`/ops/commercial/tenders/${r.tender_id}`}>{t("com.tariff.from_tender")}</Link> : <span className="text-xs text-ink-tertiary">{t("com.tariff.manual")}</span> },
  ];

  return (
    <div>
      <NeedsLine items={[
        drafts > 0 && { tone: "warning", text: t("com.tariff.drafts_n", { n: num(drafts, locale) }), onClick: () => setStatus("draft") },
        soon > 0 && { tone: "warning", icon: Clock, text: t("com.tariff.expiring_n", { n: num(soon, locale) }) },
      ]} quiet={q.data ? t("com.tariff.nothing_waiting") : null} />
      <Precedence />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented label={t("field.status")} value={status} onChange={setStatus} options={[{ value: null, label: t("com.all"), count: all.length },
          ...["draft", "active", "expired"].map((s) => ({ value: s, label: label("tariff", s), count: counts[s] || 0 }))]} />
        {canWrite && <Button size="sm" className="ml-auto" onClick={() => setCreating(true)}><Plus />{t("com.tariff.new")}</Button>}
      </div>
      <ComLoadable q={q} perm="quotes.write" isEmpty={() => rows.length === 0}
        empty={<EmptyState title={status ? t("com.empty_filtered") : t("com.tariff.empty")} body={status ? null : t("com.tariff.empty_body")} />}>
        {() => (narrow ? (
          <ul className="divide-y divide-[color:var(--border-subtle)] border-y">
            {rows.map((r) => (
              <li key={r.id}>
                <Link to={`/ops/commercial/tariffs/${r.id}`} className="flex flex-col gap-1 px-1 py-3 hover:bg-hover">
                  <span className="flex items-start justify-between gap-3">
                    <span className="min-w-0"><span className="block truncate text-sm font-medium">{r.customer || "—"}</span><span className="block truncate text-xs text-ink-tertiary">{r.name}</span></span>
                    <ComBadge group="tariff" value={r.status} />
                  </span>
                  <span className="flex flex-wrap gap-x-3 text-xs text-ink-secondary"><Validity r={r} /><span>{t("com.tariff.lanes_n", { n: num(r.lane_count, locale) })}</span></span>
                </Link>
              </li>
            ))}
          </ul>
        ) : <div className="overflow-x-auto"><DataTable columns={columns} rows={rows} dense onRowClick={(r) => navigate(`/ops/commercial/tariffs/${r.id}`)} /></div>)}
      </ComLoadable>
      {creating && <TariffDialog open={creating} onOpenChange={setCreating} onSaved={(r) => { q.reload(); if (r?.id) navigate(`/ops/commercial/tariffs/${r.id}`); }} />}
    </div>
  );
}

function Validity({ r }) {
  const { t, locale } = useT();
  const d = r.valid_to ? daysUntil(r.valid_to) : null;
  return (
    <span className="flex flex-col whitespace-nowrap text-xs">
      <span>{r.valid_from ? date(r.valid_from, locale) : "—"} – {r.valid_to ? date(r.valid_to, locale) : t("com.tariff.open_ended")}</span>
      {expiringSoon(r) && <span className="inline-flex items-center gap-1 font-medium text-warning-fg"><Clock className="size-3" aria-hidden />{t("com.tariff.ends_in", { n: num(d, locale) })}</span>}
    </span>
  );
}

export function TariffDetail() {
  const { id } = useParams();
  const q = useApi(`/tariffs/${id}`);
  return <ComLoadable q={q} perm="quotes.write">{(d) => <TariffBody tf={normTariff(d.tariff || d)} q={q} />}</ComLoadable>;
}

function TariffBody({ tf, q }) {
  const { t, locale } = useT();
  const label = useLabel();
  const { can } = useAuth();
  const canWrite = can("quotes.write");
  const narrow = useIsNarrow();
  const { confirm, dialog } = useConfirmChange();
  const [busy, setBusy] = useState(false);
  const run = (verb) => confirm(async () => {
    setBusy(true);
    try {
      const r = await apiPost(`/tariffs/${tf.id}/${verb}`, {});
      q.setData(r?.id ? r : q.data);
      q.reload();
      toast.success(t(`com.tariff.${verb}_done`));
    } catch (e) {
      toast.error(mutationError(e, t, locale));
    } finally {
      setBusy(false);
    }
  }, t(`com.tariff.${verb}_q`));
  return (
    <div>
      <PageHeader crumbs={[{ label: t("com.tab.tariffs"), to: "/ops/commercial/tariffs" }, { label: tf.customer || tf.name }]}
        title={tf.customer ? `${tf.customer} · ${tf.name}` : tf.name}
        meta={<><ComBadge group="tariff" value={tf.status} /><Validity r={tf} /><Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} /></>}
        actions={canWrite && <>
          {["draft", "scheduled", "expired"].includes(tf.status) && <Button size="sm" disabled={busy} onClick={() => run("activate")}>{t("com.tariff.activate")}</Button>}
          {tf.status === "active" && <Button size="sm" variant="destructive" disabled={busy} onClick={() => run("expire")}>{t("com.tariff.expire")}</Button>}
        </>} />
      <Precedence />
      <KV cols={3} className="mb-5" items={[
        { label: t("field.customer"), value: tf.company_id ? <Link className="text-ink-accent hover:underline" to={`/ops/customers/${tf.company_id}`}>{tf.customer || tf.company_id}</Link> : tf.customer },
        { label: t("com.tender.lanes"), value: num(tf.lane_count, locale) },
        { label: t("com.tariff.from"), value: tf.tender_id ? <Link className="text-ink-accent hover:underline" to={`/ops/commercial/tenders/${tf.tender_id}`}>{t("com.tariff.from_tender")}</Link> : t("com.tariff.manual") },
      ]} />
      {tf.lanes.length === 0 ? <p className="text-sm text-ink-tertiary">{t("com.tender.no_lanes")}</p> : narrow ? (
        <ul className="divide-y divide-[color:var(--border-subtle)] border-y">
          {tf.lanes.map((l, i) => (
            <li key={l.id || i} className="flex items-start justify-between gap-3 px-1 py-2.5">
              <span className="flex flex-col gap-1"><span className="flex items-center gap-2"><Place loc={l.origin} /><span className="text-ink-tertiary">→</span><Place loc={l.destination} /></span><ModeLabel mode={l.mode} className="text-xs" /></span>
              <span className="text-right text-sm"><Eur cents={l.price_per_unit_cents} decimals={2} /><span className="block text-xs text-ink-tertiary">/ {l.price_unit || l.volume_unit ? label("unit", l.price_unit || l.volume_unit) : t("com.unit")}</span></span>
            </li>
          ))}
        </ul>
      ) : (
        <ScrollTable label={t("com.tender.lanes")}>
          <table className="w-full text-sm">
            <thead className="text-xs text-ink-tertiary"><tr className="border-b border-[color:var(--border-subtle)]">
              <th className="px-2 py-2 text-left font-medium">{t("field.origin")}</th><th className="px-2 py-2 text-left font-medium">{t("field.destination")}</th>
              <th className="px-2 py-2 text-left font-medium">{t("field.mode")}</th><th className="px-2 py-2 text-left font-medium">{t("com.tariff.unit")}</th>
              <th className="px-2 py-2 text-right font-medium">{t("com.tender.unit_price")}</th><th className="px-2 py-2 text-right font-medium">{t("com.tariff.min")}</th>
            </tr></thead>
            <tbody>
              {tf.lanes.map((l, i) => (
                <tr key={l.id || i} className="border-b border-[color:var(--border-subtle)]">
                  <td className="px-2 py-2"><Place loc={l.origin} /></td><td className="px-2 py-2"><Place loc={l.destination} /></td>
                  <td className="px-2 py-2"><ModeLabel mode={l.mode} service={l.service} /></td>
                  <td className="px-2 py-2">{l.price_unit || l.volume_unit ? <Chip>{label("unit", l.price_unit || l.volume_unit)}</Chip> : "—"}</td>
                  <td className="px-2 py-2 text-right"><Eur cents={l.price_per_unit_cents} decimals={2} /></td>
                  <td className="px-2 py-2 text-right"><Eur cents={l.min_cents ?? l.minimum_cents ?? null} decimals={2} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollTable>
      )}
      {dialog}
    </div>
  );
}

const iso = (d) => d.toISOString().slice(0, 10);
const blankLane = () => ({ origin: "", destination: "", mode: "road", unit: "shipment", price_per_unit_cents: null, annual_volume: null });

function TariffDialog({ open, onOpenChange, onSaved }) {
  const { t, locale } = useT();
  const label = useLabel();
  const customers = useCustomerOptions(open);
  const in1y = new Date(); in1y.setFullYear(in1y.getFullYear() + 1);
  const [f, setF] = useState({ company_id: null, name: "", valid_from: iso(new Date()), valid_to: iso(in1y), lanes: [blankLane()] });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const setLane = (i, k, v) => setF((s) => ({ ...s, lanes: s.lanes.map((l, j) => (j === i ? { ...l, [k]: v } : l)) }));
  const end = (v) => { const c = v.trim().toUpperCase(); return c.length === 5 ? { unlocode: c, country: c.slice(0, 2) } : { country: c }; };
  const lanes = f.lanes.filter((l) => l.origin.trim() && l.destination.trim() && l.price_per_unit_cents !== null);
  const save = async () => {
    if (!f.company_id) { setErr(t("com.tariff.need_customer")); return; }
    if (!lanes.length) { setErr(t("com.tariff.need_lane")); return; }
    setBusy(true); setErr(null);
    try {
      const r = await apiPost("/tariffs", {
        company_id: f.company_id, name: f.name.trim() || null, valid_from: f.valid_from, valid_to: f.valid_to || null,
        lanes: lanes.map((l) => ({ origin: end(l.origin), destination: end(l.destination), mode: l.mode, volume_unit: l.unit, unit: l.unit,
          price_per_unit_cents: l.price_per_unit_cents, annual_volume: l.annual_volume })),
      });
      toast.success(t("com.tariff.created"));
      onOpenChange(false);
      onSaved?.(r);
    } catch (e) {
      setErr(mutationError(e, t, locale));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-24px)] max-w-2xl overflow-y-auto">
        <DialogHeader><DialogTitle>{t("com.tariff.new")}</DialogTitle><DialogDescription>{t("com.tariff.new_hint")}</DialogDescription></DialogHeader>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("field.customer")} htmlFor="tf-cust"><NativeSelect id="tf-cust" value={f.company_id} onChange={(v) => setF({ ...f, company_id: v })} placeholder={t("com.choose")} options={customers} /></Field>
          <Field label={t("com.tariff.name")} htmlFor="tf-name"><Input id="tf-name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
          <Field label={t("com.tariff.valid_from")} htmlFor="tf-from"><DateInput id="tf-from" value={f.valid_from} onChange={(v) => setF({ ...f, valid_from: v })} /></Field>
          <Field label={t("com.tariff.valid_to")} htmlFor="tf-to"><DateInput id="tf-to" value={f.valid_to} onChange={(v) => setF({ ...f, valid_to: v })} /></Field>
        </div>
        <fieldset className="mt-2 flex flex-col gap-2">
          <legend className="mb-1 text-xs font-medium text-ink-secondary">{t("com.tender.lanes")}</legend>
          {f.lanes.map((l, i) => (
            <div key={i} className="grid grid-cols-2 gap-2 rounded-lg p-2 shadow-[inset_0_0_0_0.5px_var(--border-subtle)] sm:grid-cols-[1fr_1fr_100px_110px_110px_32px]">
              <Input aria-label={t("field.origin")} placeholder={t("com.tariff.code_ph")} value={l.origin} onChange={(e) => setLane(i, "origin", e.target.value)} className="font-mono uppercase" />
              <Input aria-label={t("field.destination")} placeholder={t("com.tariff.code_ph")} value={l.destination} onChange={(e) => setLane(i, "destination", e.target.value)} className="font-mono uppercase" />
              <NativeSelect aria-label={t("field.mode")} value={l.mode} onChange={(v) => setLane(i, "mode", v || "road")} options={MODES.map((m) => ({ value: m, label: tEnum(t, "mode", m) }))} />
              <NativeSelect aria-label={t("com.tariff.unit")} value={l.unit} onChange={(v) => setLane(i, "unit", v || "shipment")} options={UNITS.map((u) => ({ value: u, label: label("unit", u) }))} />
              <MoneyInput aria-label={t("com.tender.unit_price")} placeholder="€" cents={l.price_per_unit_cents} onChange={(v) => setLane(i, "price_per_unit_cents", v)} />
              <Button size="icon-sm" variant="ghost" aria-label={t("com.remove")} disabled={f.lanes.length === 1} onClick={() => setF((s) => ({ ...s, lanes: s.lanes.filter((_, j) => j !== i) }))}><X /></Button>
              <div className="col-span-2 sm:hidden"><NumInput aria-label={t("com.tender.volume")} placeholder={t("com.tender.volume")} value={l.annual_volume} onChange={(v) => setLane(i, "annual_volume", v)} /></div>
            </div>
          ))}
          <Button size="sm" variant="outline" className="self-start" onClick={() => setF((s) => ({ ...s, lanes: [...s.lanes, blankLane()] }))}><Plus />{t("com.tariff.add_lane")}</Button>
        </fieldset>
        {err && <p role="alert" className="text-sm text-danger-fg">{err}</p>}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>{t("action.cancel")}</Button>
          <Button onClick={save} disabled={busy}>{busy ? t("state.saving") : t("action.save")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
