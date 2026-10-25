// Ops: new manual invoice. Customer picker, optional shipment, lines, preview totals (marked as preview),
// notes, customer reference, payment terms. "Save draft" or "Save and send".
import React, { useEffect, useMemo, useState } from "react";
import { Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import LinesEditor, { linesProblem, newLine, parseLines, PreviewTotals, toApiLines } from "./LinesEditor";

const NO_SHIPMENT = "none";

function useDebounced(value, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const h = setTimeout(() => setV(value), ms);
    return () => clearTimeout(h);
  }, [value, ms]);
  return v;
}

function CustomerPicker({ value, onChange }) {
  const { t } = useT();
  const [q, setQ] = useState("");
  const dq = useDebounced(q.trim());
  const res = useApi(value ? null : `/billing/customers${dq ? `?q=${encodeURIComponent(dq)}` : ""}`, { enabled: !value });
  if (value) {
    return (
      <div className="flex flex-wrap items-start justify-between gap-2 rounded-lg px-3 py-2 shadow-[inset_0_0_0_0.5px_var(--border-default)]">
        <div className="min-w-0">
          <p className="text-sm font-medium">{value.name}</p>
          <p className="text-xs text-ink-tertiary">
            {t("billing.fact.business_id")} {value.business_id || "—"} · {t("billing.fact.vat_id")} {value.vat_id || "—"} · {value.country || "—"}
          </p>
          <p className="text-xs text-ink-tertiary">
            {value.billing_email || t("billing.new.no_billing_email")} · {t("billing.fact.terms_days", { n: value.payment_terms_days ?? 14 })}
          </p>
        </div>
        <Button size="xs" variant="outline" onClick={() => onChange(null)}>{t("billing.new.change")}</Button>
      </div>
    );
  }
  const items = res.data?.items || [];
  return (
    <div className="flex flex-col gap-1">
      <label className="surface-input flex h-8 items-center gap-2 rounded-lg px-2.5">
        <Search className="size-4 text-ink-tertiary" aria-hidden />
        <input data-quiet-input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("billing.new.customer_search")}
          aria-label={t("billing.new.customer_search")} className="min-w-0 flex-1 bg-transparent text-md md:text-sm outline-none placeholder:text-ink-tertiary" autoFocus />
      </label>
      {res.loading && !res.data ? <LoadingRows rows={3} /> : res.error && !res.data ? <ErrorState error={res.error} onRetry={res.reload} compact /> : (
        <ul className="max-h-56 overflow-y-auto rounded-lg shadow-[inset_0_0_0_0.5px_var(--border-default)]">
          {items.length === 0 && <li className="px-3 py-2 text-sm text-ink-tertiary">{t("billing.new.no_customers")}</li>}
          {items.slice(0, 30).map((c) => (
            <li key={c.id}>
              <button type="button" onClick={() => onChange(c)} className="flex w-full flex-wrap items-baseline justify-between gap-x-3 px-3 py-1.5 text-left hover:bg-hover">
                <span className="text-sm">{c.name}</span>
                <span className="text-xs text-ink-tertiary tabular">{c.business_id || "—"} · {c.country || "—"}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ShipmentSelect({ companyId, value, onChange }) {
  const { t } = useT();
  const res = useApi(companyId ? `/shipments?company_id=${companyId}&limit=200` : null, { enabled: !!companyId });
  const items = res.data?.items || [];
  if (res.error && !res.data) return <ErrorState error={res.error} onRetry={res.reload} compact />;
  return (
    <Select value={value || NO_SHIPMENT} onValueChange={(v) => onChange(v === NO_SHIPMENT ? null : v)} disabled={!companyId || res.loading}>
      <SelectTrigger id="ni-shipment" aria-label={t("billing.new.shipment")}>
        <SelectValue placeholder={t("billing.new.no_shipment")} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={NO_SHIPMENT}>{t("billing.new.no_shipment")}</SelectItem>
        {items.map((s) => (
          <SelectItem key={s.id} value={s.id}>
            {s.reference} · {s.origin?.name || "—"} → {s.destination?.name || "—"} · {tEnum(t, "status", s.status)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export default function NewInvoiceSheet({ open, onOpenChange, onCreated }) {
  const { t } = useT();
  const vat = useApi(open ? "/invoices/vat-codes" : null, { enabled: open });
  const [customer, setCustomer] = useState(null);
  const [shipmentId, setShipmentId] = useState(null);
  const [lines, setLines] = useState(() => [newLine()]);
  const [notes, setNotes] = useState("");
  const [custRef, setCustRef] = useState("");
  const [terms, setTerms] = useState("");
  const [busy, setBusy] = useState(null); // "draft" | "send"
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!open) return;
    setCustomer(null); setShipmentId(null); setLines([newLine()]); setNotes(""); setCustRef(""); setTerms(""); setError(null); setBusy(null);
  }, [open]);
  useEffect(() => { setShipmentId(null); setTerms(customer?.payment_terms_days != null ? String(customer.payment_terms_days) : ""); }, [customer]);

  const parsed = useMemo(() => parseLines(lines), [lines]);
  const termsNum = terms === "" ? null : Number(terms);
  let problem = !customer ? t("billing.new.need_customer") : linesProblem(parsed, t);
  if (!problem && terms !== "" && (!Number.isInteger(termsNum) || termsNum < 0 || termsNum > 120)) problem = t("billing.new.bad_terms");
  const total = parsed.reduce((a, l) => a + (l.amount_cents || 0), 0);
  const sendProblem = problem || (total <= 0 ? t("billing.act.issue_zero") : null);

  const submit = async (issue) => {
    setBusy(issue ? "send" : "draft");
    setError(null);
    try {
      const inv = await apiPost("/invoices", {
        company_id: customer.id, shipment_id: shipmentId, lines: toApiLines(parsed), notes: notes.trim() || null,
        customer_reference: custRef.trim() || null, payment_terms_days: termsNum, issue, send: issue,
      });
      onCreated?.(inv, issue);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Sheet open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-2xl">
        <SheetHeader className="border-b border-[color:var(--border-subtle)] px-5 py-4 text-left">
          <SheetTitle>{t("billing.new.title")}</SheetTitle>
          <SheetDescription>{t("billing.new.subtitle")}</SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-5 py-4">
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1">
              <Label className="text-xs text-ink-secondary">{t("billing.new.customer")}</Label>
              <CustomerPicker value={customer} onChange={setCustomer} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="ni-shipment" className="text-xs text-ink-secondary">{t("billing.new.shipment")}</Label>
              <ShipmentSelect companyId={customer?.id} value={shipmentId} onChange={setShipmentId} />
              {!customer && <p className="text-xs text-ink-tertiary">{t("billing.new.shipment_after_customer")}</p>}
            </div>
            <div className="flex flex-col gap-1">
              <Label className="text-xs text-ink-secondary">{t("billing.section.lines")}</Label>
              {vat.error && !vat.data && <ErrorState error={vat.error} onRetry={vat.reload} compact />}
              <LinesEditor lines={lines} onChange={setLines} vatCodes={vat.data?.items || []} disabled={!!busy} />
            </div>
            <PreviewTotals lines={lines} vatCodes={vat.data?.items || []} />
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1">
                <Label htmlFor="ni-ref" className="text-xs text-ink-secondary">{t("billing.fact.customer_reference")}</Label>
                <Input id="ni-ref" value={custRef} onChange={(e) => setCustRef(e.target.value)} maxLength={120} disabled={!!busy} />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="ni-terms" className="text-xs text-ink-secondary">{t("billing.new.terms")}</Label>
                <Input id="ni-terms" value={terms} inputMode="numeric" onChange={(e) => setTerms(e.target.value.replace(/[^0-9]/g, ""))}
                  placeholder="14" className="tabular" disabled={!!busy} />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="ni-notes" className="text-xs text-ink-secondary">{t("billing.new.notes")}</Label>
              <Textarea id="ni-notes" value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={1000} className="min-h-[64px]" disabled={!!busy} />
            </div>
            {error && <ErrorState error={error} />}
          </div>
        </div>
        <div className="flex flex-col gap-2 border-t border-[color:var(--border-subtle)] px-5 py-3 sm:flex-row sm:items-center">
          <p className="min-h-4 text-xs text-ink-tertiary sm:mr-auto">{problem || (sendProblem ? sendProblem : t("billing.new.ready"))}</p>
          <div className="flex gap-2">
            <Button variant="outline" disabled={!!busy || !!problem} onClick={() => submit(false)} title={problem || undefined}>
              {busy === "draft" ? t("state.saving") : t("billing.new.save_draft")}
            </Button>
            <Button disabled={!!busy || !!sendProblem} onClick={() => submit(true)} title={sendProblem || undefined}>
              {busy === "send" ? t("billing.new.sending") : t("billing.new.save_send")}
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
