// Invoice lines editor (ops): description, qty, unit price in €, VAT code, pass-through.
// Used by the "new invoice" sheet and by draft editing on the ops invoice page.
import React, { useMemo } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { centsToInput, eur, parseEuro, parseQty, previewTotals, qtyToInput, vatLabel } from "./invoiceFormat";

const DISB = "OUT_OF_SCOPE_DISBURSEMENT";
const AUTO = "auto";
let seq = 0;
const nextKey = () => `l${Date.now().toString(36)}${(seq += 1)}`;

export function newLine() {
  return { key: nextKey(), description: "", qty: "1", price: "", vat_code: "", is_pass_through: false };
}

/** Invoice lines from the API -> editable rows (keeps code/unit/source so quote and customs lines stay traceable). */
export function toEditorLines(lines, locale) {
  return (lines || []).map((l) => ({
    key: l.id || nextKey(), description: l.description || "", qty: qtyToInput(l.qty ?? 1, locale),
    price: centsToInput(l.unit_price_cents, locale), vat_code: l.vat_code || "", is_pass_through: !!l.is_pass_through,
    code: l.code || null, unit: l.unit || null, source: l.source || null, source_ref: l.source_ref || null,
  }));
}

/** Parse rows -> amounts + per-field validity. */
export function parseLines(lines) {
  return lines.map((l) => {
    const qty = parseQty(l.qty);
    const unit = parseEuro(l.price);
    const amount = qty !== null && qty > 0 && unit !== null ? Math.sign(qty * unit) * Math.round(Math.abs(qty * unit)) : null;
    return {
      ...l, qtyNum: qty, unit_price_cents: unit, amount_cents: amount,
      bad: { description: !l.description.trim(), qty: qty === null || qty <= 0, price: unit === null },
    };
  });
}

/** First problem as a plain sentence (for the disabled submit reason), or null. */
export function linesProblem(parsed, t) {
  if (parsed.length === 0) return t("billing.lines.need_one");
  for (let i = 0; i < parsed.length; i += 1) {
    const p = parsed[i];
    if (p.bad.description) return t("billing.lines.err_description", { n: i + 1 });
    if (p.bad.qty) return t("billing.lines.err_qty", { n: i + 1 });
    if (p.bad.price) return t("billing.lines.err_price", { n: i + 1 });
  }
  return null;
}

export function toApiLines(parsed) {
  return parsed.map((p) => {
    const out = { description: p.description.trim(), qty: p.qtyNum, unit_price_cents: p.unit_price_cents,
      vat_code: p.is_pass_through ? DISB : (p.vat_code || null), is_pass_through: !!p.is_pass_through };
    if (p.code) out.code = p.code;
    if (p.unit) out.unit = p.unit;
    if (p.source) out.source = p.source;
    if (p.source_ref) out.source_ref = p.source_ref;
    return out;
  });
}

function VatSelect({ value, onChange, vatCodes, disabled, locale, t, id }) {
  const labels = useMemo(() => {
    const seen = {};
    (vatCodes || []).forEach((v) => { const l = vatLabel(v, locale); seen[l] = (seen[l] || 0) + 1; });
    return seen;
  }, [vatCodes, locale]);
  return (
    <Select value={value || AUTO} onValueChange={(v) => onChange(v === AUTO ? "" : v)} disabled={disabled}>
      <SelectTrigger id={id} aria-label={t("billing.col.vat_code")} className="w-full sm:w-72">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={AUTO}>{t("billing.lines.vat_auto")}</SelectItem>
        {(vatCodes || []).map((v) => {
          const l = vatLabel(v, locale);
          return <SelectItem key={v.code} value={v.code}>{labels[l] > 1 ? `${l} (${v.code})` : l}</SelectItem>;
        })}
      </SelectContent>
    </Select>
  );
}

export default function LinesEditor({ lines, onChange, vatCodes, disabled }) {
  const { t, locale } = useT();
  const parsed = useMemo(() => parseLines(lines), [lines]);
  const set = (key, patch) => onChange(lines.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  const remove = (key) => onChange(lines.filter((l) => l.key !== key));
  return (
    <div>
      <div className="hidden gap-2 pb-1 text-xs text-ink-tertiary sm:flex">
        <span className="flex-1">{t("billing.col.description")}</span>
        <span className="w-16 text-right">{t("billing.col.qty")}</span>
        <span className="w-28 text-right">{t("billing.col.unit_price_eur")}</span>
        <span className="w-28 text-right">{t("billing.col.net")}</span>
        <span className="w-7" />
      </div>
      <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] border-y border-[color:var(--border-subtle)]">
        {parsed.map((l, i) => {
          const showQtyErr = l.qty !== "" && l.bad.qty;
          const showPriceErr = l.price !== "" && l.bad.price;
          return (
            <li key={l.key} className="flex flex-col gap-2 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <Input value={l.description} disabled={disabled} onChange={(e) => set(l.key, { description: e.target.value })}
                  placeholder={t("billing.lines.description_ph")} aria-label={`${t("billing.col.description")} ${i + 1}`}
                  className="basis-full sm:basis-0 sm:flex-1" maxLength={300} />
                <Input value={l.qty} disabled={disabled} inputMode="decimal" onChange={(e) => set(l.key, { qty: e.target.value })}
                  aria-label={`${t("billing.col.qty")} ${i + 1}`} aria-invalid={showQtyErr || undefined}
                  className={cn("w-16 text-right tabular", showQtyErr && "text-danger-fg")} />
                <Input value={l.price} disabled={disabled} inputMode="decimal" onChange={(e) => set(l.key, { price: e.target.value })}
                  placeholder="0,00" aria-label={`${t("billing.col.unit_price_eur")} ${i + 1}`} aria-invalid={showPriceErr || undefined}
                  className={cn("w-28 text-right tabular", showPriceErr && "text-danger-fg")} />
                <span className="ml-auto w-28 text-right text-sm tabular sm:ml-0">{l.amount_cents === null ? <span className="text-ink-tertiary">—</span> : eur(l.amount_cents, locale)}</span>
                <Button type="button" size="icon-sm" variant="ghost" disabled={disabled} onClick={() => remove(l.key)}
                  aria-label={t("billing.lines.remove", { n: i + 1 })} title={t("billing.lines.remove", { n: i + 1 })}>
                  <Trash2 />
                </Button>
              </div>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                <VatSelect value={l.is_pass_through ? DISB : l.vat_code} vatCodes={vatCodes} locale={locale} t={t}
                  disabled={disabled || l.is_pass_through}
                  onChange={(v) => set(l.key, v === DISB ? { vat_code: v, is_pass_through: true } : { vat_code: v })} />
                <label className="inline-flex items-center gap-2 text-sm text-ink-secondary">
                  <Checkbox checked={l.is_pass_through} disabled={disabled}
                    onCheckedChange={(c) => set(l.key, c ? { is_pass_through: true, vat_code: DISB } : { is_pass_through: false, vat_code: "" })} />
                  {t("billing.lines.pass_through")}
                </label>
                {l.source && l.source !== "manual" && <span className="text-xs text-ink-tertiary">{t(`billing.source.${l.source}`)}</span>}
                {(showQtyErr || showPriceErr) && (
                  <span className="text-xs text-danger-fg">{showQtyErr ? t("billing.lines.bad_qty") : t("billing.lines.bad_price")}</span>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      <Button type="button" size="sm" variant="ghost" className="mt-2" disabled={disabled} onClick={() => onChange([...lines, newLine()])}>
        <Plus /> {t("billing.lines.add")}
      </Button>
    </div>
  );
}

/** Client-side totals, clearly marked as a preview. */
export function PreviewTotals({ lines, vatCodes }) {
  const { t, locale } = useT();
  const parsed = useMemo(() => parseLines(lines), [lines]);
  const p = useMemo(() => previewTotals(parsed, vatCodes), [parsed, vatCodes]);
  const Row = ({ label, value, strong }) => (
    <div className={cn("flex items-baseline justify-between gap-4 py-0.5", strong && "mt-1 border-t border-[color:var(--border-subtle)] pt-1.5")}>
      <dt className={cn("text-sm", strong ? "font-medium" : "text-ink-secondary")}>{label}</dt>
      <dd className={cn("text-sm tabular whitespace-nowrap", strong && "font-medium")}>{value}</dd>
    </div>
  );
  return (
    <div className="rounded-lg border border-dashed border-[color:var(--border-default)] px-3 py-2">
      <p className="mb-1 text-xs text-ink-tertiary">{t("billing.preview.label")}</p>
      <dl>
        <Row label={t("billing.total.services")} value={eur(p.services_net_cents, locale)} />
        {p.vat_breakdown.filter((b) => b.kind !== "disbursement").map((b) => (
          <Row key={b.vat_code} label={`${vatLabel(b, locale)} · ${eur(b.base_cents, locale)}`} value={eur(b.vat_cents, locale)} />
        ))}
        {p.auto_count > 0 && (
          <Row label={t("billing.preview.auto_vat", { n: p.auto_count, base: eur(p.auto_base_cents, locale) })} value={t("billing.preview.on_save")} />
        )}
        {p.pass_through_cents !== 0 && <Row label={t("billing.total.pass_through")} value={eur(p.pass_through_cents, locale)} />}
        <Row strong label={p.auto_count > 0 ? t("billing.preview.total_without_auto") : t("billing.total.total")} value={eur(p.total_cents, locale)} />
      </dl>
    </div>
  );
}
