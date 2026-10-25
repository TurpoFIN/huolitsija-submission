// Valuation: invoice total, ECB rate, freight/insurance to the EU border, post-border costs, quote lines. Broker-editable.
import React, { useEffect, useState } from "react";
import { Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { KV } from "@/components/app/layout";
import { date, money, num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { centsToInput, eurToCents, invoiceMoney, parseNum, SaveState } from "./common";

const LABEL = { freight_to_border_cents: "customs.valuation.freight", insurance_cents: "customs.valuation.insurance",
  post_border_cents: "customs.valuation.post_border" };

function Form({ val, onPatch, onClose, saveState }) {
  const { t } = useT();
  const init = () => ({
    freight_to_border_cents: centsToInput(val.freight_to_border_cents),
    insurance_cents: centsToInput(val.insurance_cents),
    post_border_cents: centsToInput(val.post_border_cents),
    rate: val.exchange_rate?.rate ? String(val.exchange_rate.rate) : "",
  });
  const [f, setF] = useState(init);
  const [err, setErr] = useState(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setF(init()), [val]);
  const set = (k) => (e) => setF((s) => ({ ...s, [k]: e.target.value }));
  const save = async () => {
    const body = {};
    for (const k of ["freight_to_border_cents", "insurance_cents", "post_border_cents"]) {
      const c = eurToCents(f[k]);
      if (Number.isNaN(c)) { setErr(`${t(LABEL[k])}: ${f[k]}`); return; }
      if (c !== (val[k] ?? null)) body[k] = c ?? 0;
    }
    const foreign = val.invoice_currency && val.invoice_currency !== "EUR";
    if (foreign && f.rate && Number(f.rate) !== val.exchange_rate?.rate) {
      const r = parseNum(f.rate);
      if (!r || Number.isNaN(r)) { setErr(`${t("customs.valuation.rate")}: ${f.rate}`); return; }
      body.exchange_rate = r;
    }
    if (!Object.keys(body).length) { onClose(); return; }
    setErr(null);
    try { await onPatch({ valuation: body }, "valuation"); onClose(); } catch (e) { setErr(e.message); }
  };
  const lbl = "text-xs text-ink-tertiary";
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <label className="flex flex-col gap-1"><span className={lbl}>{t("customs.valuation.freight")} (EUR)</span>
        <Input inputMode="decimal" value={f.freight_to_border_cents} onChange={set("freight_to_border_cents")} className="tabular" /></label>
      <label className="flex flex-col gap-1"><span className={lbl}>{t("customs.valuation.insurance")} (EUR)</span>
        <Input inputMode="decimal" value={f.insurance_cents} onChange={set("insurance_cents")} className="tabular" /></label>
      <label className="flex flex-col gap-1"><span className={lbl}>{t("customs.valuation.post_border")} (EUR)</span>
        <Input inputMode="decimal" value={f.post_border_cents} onChange={set("post_border_cents")} className="tabular" /></label>
      {val.invoice_currency && val.invoice_currency !== "EUR" && (
        <label className="flex flex-col gap-1"><span className={lbl}>{t("customs.valuation.rate_override", { currency: val.invoice_currency })}</span>
          <Input inputMode="decimal" value={f.rate} onChange={set("rate")} className="tabular" /></label>
      )}
      <div className="col-span-2 sm:col-span-4 flex items-center gap-3">
        <Button size="sm" onClick={save} disabled={saveState === "saving"}>{t("action.save")}</Button>
        <Button size="sm" variant="ghost" onClick={onClose}>{t("action.cancel")}</Button>
        {err ? <span className="text-xs text-danger-fg">{err}</span> : <SaveState state={saveState} />}
      </div>
    </div>
  );
}

export default function Valuation({ decl, canEdit, onPatch, saveState }) {
  const { t, locale } = useT();
  const [editing, setEditing] = useState(false);
  const val = decl?.valuation || {};
  const xr = val.exchange_rate;
  const lines = val.quote?.lines || [];
  const freightUnknown = val.freight_known === false;
  return (
    <div className="flex flex-col gap-4">
      <KV cols={3} items={[
        { label: t("customs.valuation.method"), value: val.method_label },
        { label: t("customs.valuation.incoterm"), value: [decl?.delivery_terms?.incoterm, decl?.delivery_terms?.place].filter(Boolean).join(" ") },
        { label: t("customs.valuation.invoice"), value: invoiceMoney(val.invoice_total, val.invoice_currency, locale),
          hint: decl?.invoice?.number ? `${decl.invoice.number}${decl.invoice.date ? ` · ${date(decl.invoice.date, locale)}` : ""}` : null },
        { label: t("customs.valuation.rate"),
          value: xr?.rate ? (val.invoice_currency === "EUR" ? "EUR" : t("customs.valuation.rate_value", { rate: num(xr.rate, locale, 4), currency: xr.currency || val.invoice_currency })) : null,
          hint: xr?.rate ? [xr.source, xr.rate_date].filter(Boolean).join(", ") : null },
        { label: t("customs.valuation.freight"),
          value: freightUnknown ? <span className="text-warning-fg">{t("customs.valuation.unknown")}</span> : money(val.freight_to_border_cents, locale, { decimals: 2 }),
          hint: val.allocation ? `${t("customs.valuation.allocation")} ${t(`customs.valuation.allocation.${val.allocation}`)}` : null },
        { label: t("customs.valuation.insurance"), value: freightUnknown ? <span className="text-warning-fg">{t("customs.valuation.unknown")}</span> : money(val.insurance_cents, locale, { decimals: 2 }) },
        { label: t("customs.valuation.post_border"), value: money(val.post_border_cents, locale, { decimals: 2 }), hint: t("customs.valuation.post_border_hint") },
      ]} />
      {xr?.rule && <p className="-mt-2 text-xs text-ink-tertiary">{xr.rule}</p>}

      {canEdit && !editing && (
        <div className="flex items-center gap-3">
          <Button size="sm" variant="outline" onClick={() => setEditing(true)}><Pencil /> {t("customs.valuation.edit")}</Button>
          <SaveState state={saveState} />
        </div>
      )}
      {editing && <Form val={val} onPatch={onPatch} onClose={() => setEditing(false)} saveState={saveState} />}

      {lines.length > 0 && (
        <div className="overflow-x-auto">
          <p className="mb-1 text-xs text-ink-tertiary">{t("customs.valuation.quote", { n: val.quote?.number || "" })}</p>
          <table className="w-full min-w-[420px] text-sm">
            <tbody>
              {lines.map((l, i) => (
                <tr key={`${l.code}-${i}`} className="border-b border-[color:var(--border-subtle)] last:border-0">
                  <td className="py-1.5 pr-2 font-mono text-[12px] text-ink-tertiary w-20">{l.code}</td>
                  <td className="py-1.5 pr-2">{l.description}</td>
                  <td className="py-1.5 pr-2 whitespace-nowrap">
                    <span className={cn("inline-flex h-5 items-center rounded-full px-1.5 text-[11px]",
                      l.bucket === "excluded" ? "text-ink-tertiary" : "bg-secondary text-ink-secondary")}>
                      {t(`customs.bucket.${l.bucket}`)}
                    </span>
                  </td>
                  <td className="py-1.5 text-right tabular whitespace-nowrap">{money(l.amount_cents, locale, { decimals: 2 })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(val.notes || []).length > 0 && (
        <div>
          <p className="text-xs text-ink-tertiary">{t("customs.valuation.notes")}</p>
          <ul className="mt-0.5 list-disc pl-4 text-xs text-ink-secondary">
            {val.notes.slice(-4).map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}
