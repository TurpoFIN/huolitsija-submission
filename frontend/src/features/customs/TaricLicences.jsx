// Licences and certificates: (1) on the import declaration, each item's TARIC licence/certificate requirements against
// the register, uploaded documents or codes declared on the item (services/customs_licences.attribute); (2) the licence
// register page (list / add / edit). Requirements and texts are the official TARIC ones; nothing is assumed met.
import React, { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertTriangle, CheckCircle2, Plus, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { DateInput } from "@/components/ui/DateInput";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar, Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Freshness, LoadingRows } from "@/components/app/states";
import { apiPatch, apiPost, useApi } from "@/lib/api";
import { dateShort, num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { useIsNarrow } from "@/features/customs/common";
import { ApiMissing, bi, goodsCode, isMissingApi } from "@/features/customs/extCommon";

// ---------------------------------------------------------------------------------------------
// Requirements on a declaration
// ---------------------------------------------------------------------------------------------
const KIND_STATE = { prohibition: "failed", licence: "pending", certificate: "pending", exemption: "expired" };

/** Summary of every item's requirements: {checked, unmet, met, unavailable, needsCode, total}. */
export function licenceSummary(decl) {
  const items = decl?.items || [];
  const out = { checked: 0, unmet: 0, met: 0, unavailable: 0, needsCode: 0, total: 0 };
  items.forEach((it) => {
    const st = it.licence_check?.status;
    if (!st) return;
    out.checked += 1;
    if (st === "unavailable") out.unavailable += 1;
    if (st === "needs_code") out.needsCode += 1;
    (it.licence_requirements || []).forEach((r) => {
      out.total += 1;
      if (r.satisfied_by) out.met += 1; else out.unmet += 1;
    });
  });
  return out;
}

function Requirement({ r }) {
  const { t, locale } = useT();
  const codes = [r.certificate_code, ...(r.alternatives || []).map((a) => a.code)].filter(Boolean);
  const s = r.satisfied_by;
  return (
    <li className="flex flex-col gap-1 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge size="sm" state={KIND_STATE[r.kind] || "expired"} label={t(`cx.lic.kind.${r.kind}`)} />
        {r.certificate_code && <span className="font-mono text-[13px] font-medium">{r.certificate_code}</span>}
        <span className="min-w-0 text-sm">{(locale === "fi" ? r.text_fi || r.text_en : r.text_en || r.text_fi) || r.measure_type_text}</span>
      </div>
      <p className="text-xs text-ink-tertiary">
        {[r.measure_type_text, r.regulation, r.geo_name || r.geo_code].filter(Boolean).join(" · ")}
        {codes.length > 1 && <> · {t("cx.lic.accepted")} <span className="font-mono">{codes.join(", ")}</span></>}
      </p>
      {s ? (
        <p className="inline-flex flex-wrap items-center gap-1 text-xs text-[color:var(--status-success-fg)]">
          <CheckCircle2 className="size-3.5" aria-hidden />
          {t(`cx.lic.by.${s.source}`)} <span className="font-mono">{s.code} {s.reference || ""}</span>
          {s.valid_to && <span className="text-ink-tertiary">· {t("cx.lic.valid_to", { date: dateShort(s.valid_to, locale) })}</span>}
          {s.source === "register" && <Link to="/ops/customs/licences" className="text-ink-accent hover:underline">{t("cx.lic.open_register")}</Link>}
        </p>
      ) : (
        <p className="inline-flex flex-wrap items-center gap-1 text-xs text-danger-fg">
          <XCircle className="size-3.5" aria-hidden />
          {r.kind === "prohibition" && !codes.length ? t("cx.lic.prohibited") : t("cx.lic.missing")}
          {r.register_note && <span className="text-ink-secondary">· {r.register_note}</span>}
          {r.fallback_action && <span className="text-ink-tertiary">· {r.fallback_action}</span>}
        </p>
      )}
    </li>
  );
}

export function LicenceRequirements({ decl }) {
  const { t } = useT();
  const sum = licenceSummary(decl);
  const items = decl?.items || [];
  if (!sum.checked) return <p className="text-sm text-ink-tertiary">{t("cx.lic.not_checked")}</p>;
  const lead = sum.needsCode ? { tone: "warn", Icon: AlertTriangle, text: t("cx.lic.needs_code") } : sum.unavailable ? { tone: "warn", Icon: AlertTriangle, text: t("cx.lic.unavailable") }
    : sum.unmet ? { tone: "bad", Icon: XCircle, text: t("cx.lic.unmet_n", { n: sum.unmet }) }
      : sum.total ? { tone: "ok", Icon: CheckCircle2, text: t("cx.lic.all_met", { n: sum.total }) }
        : { tone: "ok", Icon: CheckCircle2, text: t("cx.lic.none_required") };
  return (
    <div>
      <p className={cn("mb-1 inline-flex items-center gap-1.5 text-sm", lead.tone === "bad" ? "text-danger-fg" : lead.tone === "warn" ? "text-warning-fg" : "text-ink-secondary")}>
        <lead.Icon className={cn("size-4", lead.tone === "ok" && "text-[color:var(--status-success-fg)]")} aria-hidden />{lead.text}
      </p>
      <ul className="divide-y divide-[color:var(--border-subtle)]">
        {items.filter((it) => (it.licence_requirements || []).length || ["unavailable", "needs_code"].includes(it.licence_check?.status)).map((it) => (
          <li key={it.seq} className="py-2">
            <p className="text-xs text-ink-tertiary"><span className="font-mono">#{it.seq}</span> · {it.description || "—"} · <span className="font-mono">{goodsCode(it.taric_code || it.cn_code)}</span></p>
            {it.licence_check?.status === "unavailable" && <p className="text-xs text-warning-fg">{t("cx.lic.unavailable_item")}</p>}
            {it.licence_check?.status === "needs_code" && <p className="text-xs text-warning-fg">{t("cx.lic.needs_code")}</p>}
            <ul className="divide-y divide-[color:var(--border-subtle)]">
              {(it.licence_requirements || []).map((r, i) => <Requirement key={`${r.measure_sid}-${r.condition_code}-${i}`} r={r} />)}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Register page
// ---------------------------------------------------------------------------------------------
const STATUS_STATE = { active: "success", suspended: "pending", expired: "expired", revoked: "failed" };
const STATUSES = ["active", "suspended", "expired", "revoked"];
export const daysTo = (iso) => {
  if (!iso) return null;
  const days = (new Date(`${iso}T23:59:59`) - Date.now()) / 86400000;
  return days < 0 ? Math.floor(days) : Math.ceil(days);
};

export function licenceRows(data) {
  if (!data) return [];
  return Array.isArray(data) ? data : data.items || data.licences || [];
}

function Balance({ l }) {
  const { t, locale } = useT();
  if (l.quantity_total === null || l.quantity_total === undefined) return <span className="text-ink-tertiary">{t("cx.missing")}</span>;
  const low = l.quantity_balance !== null && l.quantity_balance !== undefined && l.quantity_total > 0 && l.quantity_balance / l.quantity_total < 0.1;
  return (
    <span className={cn("tabular whitespace-nowrap", low && "text-warning-fg")}>
      {l.quantity_balance == null ? t("cx.missing") : num(l.quantity_balance, locale, 0)} / {num(l.quantity_total, locale, 0)} {l.unit || ""}
    </span>
  );
}

function Validity({ l }) {
  const { t, locale } = useT();
  const d = daysTo(l.valid_to);
  return (
    <span className="inline-flex flex-col">
      <span className="tabular whitespace-nowrap">{l.valid_from ? dateShort(l.valid_from, locale) : "…"} – {l.valid_to ? dateShort(l.valid_to, locale) : "…"}</span>
      {d !== null && d >= 0 && d <= 30 && <span className="text-[11px] text-warning-fg">{t("cx.lic.expires_in", { n: d })}</span>}
      {d !== null && d < 0 && <span className="text-[11px] text-ink-tertiary">{t("cx.lic.expired")}</span>}
    </span>
  );
}

const EMPTY = { company_id: "", certificate_code: "", number: "", issuer: "", holder: "", goods: "", quantity_total: "", quantity_balance: "",
  unit: "", valid_from: "", valid_to: "", status: "active", note: "" };

function toForm(l) {
  if (!l) return { ...EMPTY };
  return { ...EMPTY, ...Object.fromEntries(Object.keys(EMPTY).map((k) => [k, l[k] === null || l[k] === undefined ? "" : l[k]])),
    goods: (l.goods || []).join(", ") };
}

function LicenceDialog({ open, onOpenChange, licence, companies, onSaved }) {
  const { t } = useT();
  const [form, setForm] = useState(toForm(licence));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => { if (open) { setForm(toForm(licence)); setError(null); } }, [open, licence]);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e?.target ? e.target.value : e }));
  const save = async () => {
    const body = { ...form, goods: form.goods.split(/[\s,;]+/).filter(Boolean),
      quantity_total: form.quantity_total === "" ? null : Number(String(form.quantity_total).replace(",", ".")),
      quantity_balance: form.quantity_balance === "" ? null : Number(String(form.quantity_balance).replace(",", ".")) };
    if (!licence) delete body.quantity_balance;
    ["issuer", "holder", "unit", "valid_from", "valid_to", "note"].forEach((k) => { if (body[k] === "") body[k] = null; });
    setBusy(true); setError(null);
    try {
      const saved = licence ? await apiPatch(`/customs/licences/${licence.id}`, body) : await apiPost("/customs/licences", body);
      onOpenChange(false);
      await onSaved?.(saved);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const field = (k, label, props = {}) => (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-ink-tertiary">{label}</span>
      <Input value={form[k]} onChange={set(k)} disabled={busy} {...props} />
    </label>
  );
  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{licence ? t("cx.lic.edit_title", { number: licence.number }) : t("cx.lic.add_title")}</DialogTitle>
          <DialogDescription>{t("cx.lic.dialog_body")}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1 sm:col-span-2">
            <span className="text-xs text-ink-tertiary">{t("cx.lic.f.company")}</span>
            <select value={form.company_id} onChange={set("company_id")} disabled={busy || !!licence}
              className="surface-input h-8 rounded-lg bg-transparent px-2 text-md md:text-sm">
              <option value="">{t("cx.lic.f.company_pick")}</option>
              {(companies || []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </label>
          {field("certificate_code", t("cx.lic.f.code"), { className: "font-mono uppercase", placeholder: "C052", maxLength: 4 })}
          {field("number", t("cx.lic.f.number"))}
          {field("issuer", t("cx.lic.f.issuer"))}
          {field("holder", t("cx.lic.f.holder"))}
          <label className="flex flex-col gap-1 sm:col-span-2">
            <span className="text-xs text-ink-tertiary">{t("cx.lic.f.goods")}</span>
            <Input value={form.goods} onChange={set("goods")} disabled={busy} className="font-mono" placeholder="8712, 7208100000" />
          </label>
          {field("quantity_total", t("cx.lic.f.total"), { inputMode: "decimal", className: "tabular" })}
          {licence ? field("quantity_balance", t("cx.lic.f.balance"), { inputMode: "decimal", className: "tabular" }) : field("unit", t("cx.lic.f.unit"), { placeholder: "kg" })}
          {licence && field("unit", t("cx.lic.f.unit"), { placeholder: "kg" })}
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-tertiary">{t("cx.lic.f.from")}</span>
            <DateInput value={form.valid_from} onChange={set("valid_from")} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-tertiary">{t("cx.lic.f.to")}</span>
            <DateInput value={form.valid_to} onChange={set("valid_to")} />
          </label>
          {licence && (
            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-tertiary">{t("cx.lic.f.status")}</span>
              <select value={form.status} onChange={set("status")} disabled={busy} className="surface-input h-8 rounded-lg bg-transparent px-2 text-md md:text-sm">
                {STATUSES.map((s) => <option key={s} value={s}>{t(`cx.lic.st.${s}`)}</option>)}
              </select>
            </label>
          )}
          <label className="flex flex-col gap-1 sm:col-span-2">
            <span className="text-xs text-ink-tertiary">{t("cx.lic.f.note")}</span>
            <Textarea value={form.note} onChange={set("note")} rows={2} disabled={busy} />
          </label>
        </div>
        {error && <ErrorState error={error} compact />}
        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>{t("action.cancel")}</Button>
          <Button onClick={save} disabled={busy || !form.company_id || !form.certificate_code || !form.number}>{busy ? t("state.saving") : t("action.save")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function LicenceRegister() {
  const { t, locale } = useT();
  const narrow = useIsNarrow();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const status = params.get("status") || "active";
  const q = useApi("/customs/licences?limit=500");
  const companies = useApi("/ops/customers");
  const [edit, setEdit] = useState(null); // null closed | {} new | licence
  const compList = useMemo(() => companies.data?.items || [], [companies.data]);
  const compName = useMemo(() => Object.fromEntries(compList.map((c) => [c.id, c.name])), [compList]);
  const all = licenceRows(q.data);
  const s = search.trim().toLowerCase();
  const rows = all.filter((l) => (status === "all" || l.status === status) && (!s || [l.certificate_code, l.number, l.issuer, l.holder, compName[l.company_id], ...(l.goods || [])]
    .filter(Boolean).some((x) => String(x).toLowerCase().includes(s))));
  const expiring = all.filter((l) => l.status === "active" && daysTo(l.valid_to) !== null && daysTo(l.valid_to) >= 0 && daysTo(l.valid_to) <= 30).length;
  const counts = Object.fromEntries(STATUSES.map((k) => [k, all.filter((l) => l.status === k).length]));

  if (isMissingApi(q.error, { any404: true })) return <ApiMissing />;
  const columns = [
    { key: "certificate_code", header: t("cx.lic.col.code"), sort: (l) => l.certificate_code,
      cell: (l) => <span className="inline-flex flex-col"><span className="font-mono text-[13px] font-medium">{l.certificate_code}</span><span className="text-[11px] text-ink-tertiary">{l.number}</span></span> },
    { key: "company", header: t("field.customer"), sort: (l) => compName[l.company_id] || "",
      cell: (l) => <span className="inline-flex flex-col"><span>{compName[l.company_id] || "—"}</span><span className="text-[11px] text-ink-tertiary">{l.holder || ""}</span></span> },
    { key: "issuer", header: t("cx.lic.col.issuer"), hideBelow: "lg", cell: (l) => l.issuer || <span className="text-ink-tertiary">—</span> },
    { key: "goods", header: t("cx.lic.col.goods"), cell: (l) => <span className="font-mono text-xs">{(l.goods || []).join(", ") || "—"}</span> },
    { key: "balance", header: t("cx.lic.col.balance"), align: "right", sort: (l) => l.quantity_balance, cell: (l) => <Balance l={l} /> },
    { key: "valid_to", header: t("cx.lic.col.valid"), sort: (l) => l.valid_to || "", cell: (l) => <Validity l={l} /> },
    { key: "usage", header: t("cx.lic.col.usage"), align: "right", hideBelow: "lg", sort: (l) => (l.usage || []).length, cell: (l) => (l.usage || []).length },
    { key: "status", header: t("field.status"), sort: (l) => l.status, cell: (l) => <StatusBadge size="sm" state={STATUS_STATE[l.status] || "expired"} label={t(`cx.lic.st.${l.status}`)} /> },
  ];
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-sm text-ink-secondary">
        {q.data && <span>{t("cx.lic.meta.active", { n: counts.active || 0 })}</span>}
        {expiring > 0 && <span className="text-warning-fg">· {t("cx.lic.meta.expiring", { n: expiring })}</span>}
        <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
      </div>
      <FilterBar search={search} onSearch={setSearch} placeholder={t("cx.lic.search")}
        right={<Button onClick={() => setEdit({})}><Plus /> {t("cx.lic.add")}</Button>} />
      <div className="mb-3">
        <Segmented value={status} onChange={(v) => { const n = new URLSearchParams(params); n.set("status", v); setParams(n, { replace: true }); }} label={t("field.status")}
          options={[...STATUSES.map((k) => ({ value: k, label: t(`cx.lic.st.${k}`), count: q.data ? counts[k] : undefined })), { value: "all", label: t("cx.lic.st.all"), count: q.data ? all.length : undefined }]} />
      </div>
      {q.error && q.data && <ErrorState className="mb-3" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
      {q.loading && !q.data ? <LoadingRows rows={6} /> : q.error && !q.data ? <ErrorState error={q.error} onRetry={q.reload} /> : rows.length === 0 ? (
        <EmptyState title={s ? t("cx.lic.empty.search", { q: search.trim() }) : t("cx.lic.empty.title")} body={s ? null : t("cx.lic.empty.body")}
          action={s ? null : <Button variant="outline" onClick={() => setEdit({})}><Plus /> {t("cx.lic.add")}</Button>} />
      ) : narrow ? (
        <ul className="divide-y divide-[color:var(--border-subtle)]">
          {rows.map((l) => (
            <li key={l.id}>
              <button type="button" onClick={() => setEdit(l)} className="flex w-full flex-col gap-1 py-3 text-left">
                <span className="flex items-center justify-between gap-2">
                  <span className="font-mono text-[13px] font-medium">{l.certificate_code} <span className="font-normal text-ink-tertiary">{l.number}</span></span>
                  <StatusBadge size="sm" state={STATUS_STATE[l.status] || "expired"} label={t(`cx.lic.st.${l.status}`)} />
                </span>
                <span className="text-sm">{compName[l.company_id] || "—"}</span>
                <span className="flex flex-wrap gap-x-3 text-xs text-ink-secondary"><Balance l={l} /><Validity l={l} /></span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <DataTable dense columns={columns} rows={rows} onRowClick={(l) => setEdit(l)} />
      )}
      <LicenceDialog open={!!edit} onOpenChange={(o) => !o && setEdit(null)} licence={edit && edit.id ? edit : null} companies={compList}
        onSaved={async () => { await q.reload(); }} />
      {locale && null}
    </div>
  );
}

export default LicenceRegister;
// bi is re-exported for callers that show TARIC texts in the UI language.
export { bi };
