import React, { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Section } from "@/components/app/layout";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { apiPatch, fileUrl, useApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { ApiMissing, isMissingApi } from "./extCommon";

const FIELDS = ["customs_four_eyes_duty_eur", "customs_four_eyes_value_eur", "customs_rescreen_days", "record_retention_years"];
export function ControlsKpis({ data }) {
  const { t, locale } = useT();
  return <Section title={t("cx.ctl.kpis")}>
    <p className="mb-3 text-xs text-ink-secondary">{data.period.from} – {data.period.to}</p>
    <dl className="divide-y divide-border max-w-2xl">{[...Object.entries(data.totals).filter(([k]) => ["filed", "rejected", "released", "amendments", "rejection_rate_pct"].includes(k)), ["release_hours", data.totals.time_to_release_hours?.median]].map(([key, value]) => <div key={key} className="flex justify-between gap-4 py-2 text-sm"><dt>{t(`cx.ctl.${key}`)}</dt><dd className="tabular">{value == null ? t("cx.missing") : new Intl.NumberFormat(locale).format(value)}</dd></div>)}</dl>
  </Section>;
}
export default function ControlsSettings() {
  const { t, locale } = useT();
  const q = useApi("/customs/controls/settings");
  const staff = useApi("/ops/users");
  const kpi = useApi("/customs/controls/kpis");
  const [form, setForm] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => { if (q.data) setForm(Object.fromEntries([...FIELDS, "customs_authorised_filers"].map(k => [k, q.data[k]]))); }, [q.data]);
  const change = (k, v) => { setSaved(false); setForm(f => ({ ...f, [k]: v })); };
  async function save(e) {
    e.preventDefault(); setBusy(true); setError(null); setSaved(false);
    try { await apiPatch("/customs/controls/settings", { ...form, ...Object.fromEntries(FIELDS.map(k => [k, Number(form[k])])) }); await q.reload(); setSaved(true); }
    catch (e) { setError(e); } finally { setBusy(false); }
  }
  if (isMissingApi(q.error, { any404: true })) return <ApiMissing />;
  return <div>
    <Section title={t("cx.ctl.settings")}>
      {q.error && <ErrorState error={q.error} onRetry={q.reload} />}
      {!form && q.loading && <LoadingRows rows={5} />}
      {form && <form onSubmit={save} className="max-w-2xl">
        <p className="mb-3 text-xs text-ink-secondary">{t("cx.ctl.threshold_help")}</p>
        <div className="divide-y divide-border">{FIELDS.map(k => <label key={k} className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm"><span>{t(`cx.ctl.${k}`)}</span><Input className="w-36 tabular" type="number" required min={k === "record_retention_years" ? 6 : 0} step={k.includes("eur") ? "0.01" : "1"} value={form[k] ?? ""} onChange={e => change(k, e.target.value)} disabled={busy} /></label>)}</div>
        <fieldset className="my-4"><legend className="mb-1 text-sm">{t("cx.ctl.filers")}</legend><p className="mb-2 text-xs text-ink-secondary">{t("cx.ctl.all_filers")}</p>
          {[...new Map([...(staff.data?.users || []).filter(u => !u.disabled && u.permissions?.includes("customs.file")), ...(q.data.eligible_filers || []), ...(q.data.authorised_filers || [])].map(u => [u.id, u])).values()].map(u => <label key={u.id} className="flex items-center gap-2 py-1 text-sm"><input type="checkbox" disabled={busy} checked={(form.customs_authorised_filers || []).includes(u.id)} onChange={e => change("customs_authorised_filers", e.target.checked ? [...(form.customs_authorised_filers || []), u.id] : form.customs_authorised_filers.filter(id => id !== u.id))} />{u.name || u.email || u.id}</label>)}
        </fieldset>
        {error && <ErrorState error={error} compact />}
        <div className="flex items-center gap-3"><Button disabled={busy} type="submit">{t(busy ? "state.saving" : "action.save")}</Button>{saved && <span role="status" className="text-sm text-success-fg">{t("cx.ctl.saved")}</span>}</div>
      </form>}
    </Section>
    {kpi.error && <ErrorState error={kpi.error} onRetry={kpi.reload} />}
    {kpi.data && <ControlsKpis data={kpi.data} />}
    <Button variant="outline" asChild><a href={fileUrl("/customs/controls/audit.csv")} download>{t("cx.ctl.audit")}</a></Button>
  </div>;
}
