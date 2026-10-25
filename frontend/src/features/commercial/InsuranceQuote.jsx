// Cargo insurance quote + bind for one shipment. Every price says which insurer made it; without the insurer contract the
// API answers insurer_not_configured and the form says so instead of quoting.
import React, { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { FileCheck2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { KV } from "@/components/app/layout";
import { apiPost, fileUrl } from "@/lib/api";
import { dateTime, money, pct } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Field, MoneyInput, NativeSelect } from "@/features/quotes/ops/common";
import { COVERS, ComBadge, Eur, ProviderMark, SourceLink, mutationError, useLabel } from "./common";
import { policy as normPolicy } from "./shapes";

export { COVERS, ProviderMark };

export function InsuranceQuoteForm({ shipmentId, defaultValueCents, providers = [], onBound, shipmentPicker }) {
  const { t, locale } = useT();
  const label = useLabel();
  const [cover, setCover] = useState("ICC_A");
  const [value, setValue] = useState(defaultValueCents ?? null);
  const [quote, setQuote] = useState(null);
  const [busy, setBusy] = useState(null);
  const [err, setErr] = useState(null);
  const sid = shipmentId;
  const active = providers.find((p) => p.active) || providers[0] || null;
  const ask = async (e) => {
    e.preventDefault();
    if (!sid) { setErr(t("com.ins.need_shipment")); return; }
    if (!value) { setErr(t("com.ins.need_value")); return; }
    setBusy("quote"); setErr(null); setQuote(null);
    try {
      setQuote(await apiPost("/insurance/quote", { shipment_id: sid, cover, insured_value_cents: value, currency: "EUR" }));
    } catch (x) {
      setErr(mutationError(x, t, locale));
    } finally {
      setBusy(null);
    }
  };
  const bind = async () => {
    setBusy("bind"); setErr(null);
    try {
      const pol = normPolicy(await apiPost(`/insurance/${quote.id}/bind`, {}));
      toast.success(t("com.ins.bound", { no: pol.certificate_number || "" }));
      setQuote(null);
      onBound?.(pol);
    } catch (x) {
      setErr(mutationError(x, t, locale));
    } finally {
      setBusy(null);
    }
  };
  const qStatus = quote?.provider_mode || providers.find((p) => p.id === quote?.provider)?.status || active?.status;
  const blocked = active && active.status !== "live";
  return (
    <div className="flex flex-col gap-3">
      <form onSubmit={ask} className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_140px_160px_auto] sm:items-end">
        {shipmentPicker || <span className="hidden sm:block" />}
        <Field label={t("com.ins.cover")} htmlFor={`ic-${sid || "x"}`}>
          <NativeSelect id={`ic-${sid || "x"}`} value={cover} onChange={(v) => setCover(v || "ICC_A")} options={COVERS.map((c) => ({ value: c, label: label("cover", c) }))} />
        </Field>
        <Field label={t("com.ins.value")} htmlFor={`iv-${sid || "x"}`}><MoneyInput id={`iv-${sid || "x"}`} cents={value} onChange={setValue} /></Field>
        <Button type="submit" size="sm" disabled={!!busy || blocked}>{busy === "quote" ? t("com.ins.quoting") : t("com.ins.quote")}</Button>
      </form>
      <p className="text-xs text-ink-tertiary">{t(`com.ins.cover_hint.${cover}`)}</p>
      {blocked && <p className="text-xs text-warning-fg">{t("com.ins.blocked_warning", { what: (locale === "fi" ? active.blocked_on_fi : active.blocked_on) || active.name })}</p>}
      {err && <p role="alert" className="text-sm text-danger-fg">{err}</p>}
      {quote && (
        <div className="rounded-lg p-3 shadow-[inset_0_0_0_0.5px_var(--border-default)]" aria-live="polite">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <ProviderMark status={qStatus} name={quote.provider_name || quote.provider} />
          </div>
          <KV cols={2} items={[
            { label: t("com.ins.premium"), value: <Eur cents={quote.premium_cents} decimals={2} /> },
            { label: t("com.ins.rate"), value: quote.rate_pct !== undefined && quote.rate_pct !== null ? pct(quote.rate_pct, locale, 2) : null },
            { label: t("com.ins.cover"), value: label("cover", quote.cover || cover) },
            { label: t("com.ins.value"), value: money(quote.insured_value_cents ?? value, locale) },
            { label: t("com.ins.deductible"), value: quote.deductible_cents !== undefined && quote.deductible_cents !== null ? <Eur cents={quote.deductible_cents} /> : null },
            { label: t("com.ins.valid_until"), value: quote.expires_at ? dateTime(quote.expires_at, locale) : null },
          ]} />
          {quote.terms_url && <p className="mt-2 text-xs"><SourceLink href={quote.terms_url}>{t("com.ins.terms")}</SourceLink></p>}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={bind} disabled={!!busy}><FileCheck2 />{busy === "bind" ? t("state.saving") : t("com.ins.bind")}</Button>
            <Button size="sm" variant="ghost" onClick={() => setQuote(null)}>{t("action.cancel")}</Button>
          </div>
        </div>
      )}
    </div>
  );
}

export function PolicyList({ items, showShipment }) {
  const { t, locale } = useT();
  const label = useLabel();
  if (!items.length) return <p className="text-sm text-ink-tertiary">{t("com.ins.no_policies")}</p>;
  return (
    <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] border-y">
      {items.map((p) => (
        <li key={p.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2 text-sm">
          <span className="min-w-[140px] font-mono text-[13px]">{p.certificate_number || p.id}</span>
          <ComBadge group="policy" value={p.status} />
          <span>{label("cover", p.cover)}</span>
          <span className="text-ink-secondary">{t("com.ins.value")} <Eur cents={p.insured_value_cents} /></span>
          <span className="text-ink-secondary">{t("com.ins.premium")} <Eur cents={p.premium_cents} decimals={2} /></span>
          <ProviderMark status={p.provider_mode || "live"} name={p.provider_label || p.provider} />
          {showShipment && p.shipment_id && <Link className="text-ink-accent hover:underline" to={`/ops/shipments/${p.shipment_id}`}>{p.shipment_reference || t("com.ins.shipment")}</Link>}
          {p.certificate_url && <a className="text-ink-accent hover:underline" href={p.certificate_url.startsWith("/api/") ? fileUrl(p.certificate_url.slice(4)) : p.certificate_url} target="_blank" rel="noreferrer">{t("com.ins.certificate")}</a>}
          <span className="ml-auto text-xs text-ink-tertiary">{p.at ? dateTime(p.at, locale) : ""}</span>
        </li>
      ))}
    </ul>
  );
}
