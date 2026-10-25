// Ops › Finance › VAT (ALV). Decision: "what VAT do we pay (or get back) for this period, does anything need checking
// before filing, and what goes on the EU sales list?" The periodic tax return rows carry their Finnish (OmaVero) labels;
// exports as CSV or the Vero file format.
// APIs: GET /finance/reports/vat?period=YYYY-MM, GET /finance/reports/vat/export?period=&format=csv|vero,
// GET /finance/reports/ec-sales?period=[&format=csv|vero].
import React from "react";
import { useSearchParams } from "react-router-dom";
import { Section } from "@/components/app/layout";
import { Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Dot, FileLink, FinBar, MonthStepper } from "@/features/finance/kit";
import { FinTable } from "@/features/finance/Table";
import { eur, first, pairText, periodLabel, qs } from "@/features/finance/format";

const MONTHLY = /^\d{4}-\d{2}$/;

/** One return row -> {code, label, base, vat, amount}. */
export function readVatRow(r, locale) {
  return {
    code: first(r.code, r.box, r.field, r.key),
    label: pairText(r, "label", locale) || first(r.name, r.code, r.key),
    base: first(r.base_cents, r.net_cents, r.taxable_cents),
    vat: first(r.vat_cents, r.tax_cents),
    amount: first(r.amount_cents, r.value_cents, r.vat_cents, r.tax_cents, r.base_cents),
    note: pairText(r, "note", locale),
  };
}

function Verify({ items }) {
  const { locale } = useT();
  if (!items || (Array.isArray(items) && items.length === 0)) return null;
  const list = Array.isArray(items) ? items : [items];
  return (
    <ul className="mb-4 flex flex-col gap-1">
      {list.map((v, i) => (
        <li key={i} className="rounded-lg px-3 py-2 text-sm status-pending">
          {typeof v === "string" ? v : pairText(v, "message", locale) || pairText(v, "text", locale) || (locale === "fi" ? v.fi : v.en) || v.fi || v.en || JSON.stringify(v)}
        </li>
      ))}
    </ul>
  );
}

export default function Vat() {
  const { t, locale } = useT();
  const [params, setParams] = useSearchParams();
  const asked = params.get("period") || null;
  const q = useApi(`/finance/reports/vat${qs({ period: asked })}`);
  const period = asked || q.data?.period || null;
  const ec = useApi(period ? `/finance/reports/ec-sales${qs({ period })}` : null, { enabled: !!period });
  const setPeriod = (p) => {
    const next = new URLSearchParams(params);
    next.set("period", p);
    setParams(next, { replace: true });
  };

  const rows = (q.data?.rows || q.data?.items || q.data?.boxes || []).map((r) => readVatRow(r, locale));
  const payable = first(q.data?.tax_payable_cents, q.data?.payable_cents);
  const ecItems = ec.data?.items || [];
  return (
    <div className="max-w-[1152px]">
      <FinBar title={t("fin.tab.vat")}
        meta={<>
          {period && MONTHLY.test(period) ? <MonthStepper period={period} onChange={setPeriod} /> : <span className="font-medium text-foreground">{period || "—"}</span>}
          {payable !== null && (
            <>
              <Dot />
              <span>{payable < 0 ? t("fin.vat.refundable") : t("fin.vat.payable")}</span>
              <span className="tabular font-medium text-foreground">{eur(Math.abs(payable), locale)}</span>
            </>
          )}
          {q.data?.status && <><Dot /><span className="text-xs">{tEnum(t, "fin.vat_status", q.data.status)}</span></>}
          {q.data?.due_date && <span className="text-xs text-ink-tertiary">{t("fin.vat.due", { date: q.data.due_date })}</span>}
          <Freshness at={q.updatedAt} onRefresh={() => { q.reload(); ec.reload(); }} refreshing={q.refreshing} />
        </>}
        actions={period ? (
          <>
            <FileLink path={`/finance/reports/vat/export${qs({ period, format: "csv" })}`} label="CSV" size="sm" />
            <FileLink path={`/finance/reports/vat/export${qs({ period, format: "vero" })}`} label={t("fin.vat.vero_file")} size="sm" />
          </>
        ) : null} />
      <Loadable q={q} skeleton={<LoadingRows rows={10} />}>
        {(d) => (
          <>
            <Verify items={d.verify} />
            <Section title={locale === "fi" ? "Tuonnin ALV maahantuojittain" : "Import VAT by importer"}>
              <FinTable rows={d.import_tilitys || []} rowKey="declaration_id" columns={[
                { key: "importer", header: locale === "fi" ? "Maahantuoja" : "Importer", cell: r => r.importer || (locale === "fi" ? "Tieto puuttuu" : "Missing data") },
                { key: "mrn", header: "MRN", cell: r => r.mrn },
                { key: "base", header: t("fin.vat.base"), align: "right", cell: r => eur(r.vat_base_cents, locale) },
                { key: "vat", header: t("fin.vat.tax"), align: "right", cell: r => eur(r.vat_cents, locale) },
                { key: "deductible", header: locale === "fi" ? "Vähennettävä" : "Deductible", align: "right", cell: r => eur(r.deductible_vat_cents, locale) },
                { key: "own", header: locale === "fi" ? "Ilmoittaja" : "Return owner", cell: r => r.own_return == null ? (locale === "fi" ? "Tieto puuttuu" : "Missing data") : r.own_return ? (locale === "fi" ? "Huolitsija" : "Forwarder") : (locale === "fi" ? "Asiakas" : "Customer") }
              ]} />
            </Section>
            <Section title={t("fin.vat.return", { period: period && MONTHLY.test(period) ? periodLabel(period, locale) : period || "" })}>
              {rows.length === 0 ? <p className="py-2 text-sm text-ink-tertiary">{t("fin.vat.no_rows")}</p> : (
                <FinTable rows={rows} rowKey={(r) => `${r.code}-${r.label}`} totals={payable !== null ? { payable } : null} totalsLabel={payable < 0 ? t("fin.vat.refundable") : t("fin.vat.payable")}
                  columns={[
                    { key: "code", header: t("fin.vat.code"), sortable: false, className: "w-16 text-ink-tertiary tabular", cell: (r) => r.code ?? "" },
                    { key: "label", header: t("fin.vat.row"), sortable: false, cell: (r) => <span className="block min-w-[14rem]">{r.label}{r.note && <span className="block text-[11px] text-ink-tertiary">{r.note}</span>}</span> },
                    ...(rows.some((r) => r.base !== null && r.vat !== null) ? [
                      { key: "base", header: t("fin.vat.base"), align: "right", sortable: false, hideBelow: "md", cell: (r) => (r.base === null ? "" : eur(r.base, locale)) },
                      { key: "vat", header: t("fin.vat.tax"), align: "right", sortable: false, cell: (r) => (r.vat === null ? "" : eur(r.vat, locale)),
                        total: (x) => <span className="font-medium">{eur(Math.abs(x.payable), locale)}</span> },
                    ] : [
                      { key: "amount", header: t("fin.col.amount"), align: "right", sortable: false, cell: (r) => eur(r.amount, locale),
                        total: (x) => <span className="font-medium">{eur(Math.abs(x.payable), locale)}</span> },
                    ]),
                  ]} />
              )}
              {d.basis_note_fi && <p className="mt-2 text-xs text-ink-tertiary">{locale === "fi" ? d.basis_note_fi : d.basis_note_en || d.basis_note_fi}</p>}
            </Section>
          </>
        )}
      </Loadable>
      <Section title={t("fin.vat.ec_sales")}
        actions={period ? (
          <>
            <FileLink path={`/finance/reports/ec-sales${qs({ period, format: "csv" })}`} label="CSV" />
            <FileLink path={`/finance/reports/ec-sales${qs({ period, format: "vero" })}`} label={t("fin.vat.vero_file")} />
          </>
        ) : null}>
        <Loadable q={ec} skeleton={<LoadingRows rows={4} />} isEmpty={(d) => (d.items || []).length === 0}
          empty={<p className="py-2 text-sm text-ink-tertiary">{t("fin.vat.ec_empty")}</p>}>
          {(d) => (
            <FinTable rows={ecItems} rowKey={(r) => `${r.vat_id || r.customer_vat_id}-${r.kind || ""}`}
              totals={d.totals || (d.total_cents !== undefined ? { amount_cents: d.total_cents } : null)} totalsLabel={t("fin.total")}
              columns={[
                { key: "country", header: t("fin.vat.country"), cell: (r) => first(r.country, r.country_code) || "—" },
                { key: "vat_id", header: t("fin.ci.vat_id"), cell: (r) => <span className="font-mono text-[13px] whitespace-nowrap">{first(r.vat_id, r.customer_vat_id) || "—"}</span> },
                { key: "customer", header: t("field.customer"), hideBelow: "md", cell: (r) => <span className="block max-w-[16rem] truncate">{first(r.buyer, r.company_name, r.customer_name, r.name) || "—"}</span> },
                { key: "kind", header: t("fin.vat.kind"), hideBelow: "md", cell: (r) => (r.kind ? tEnum(t, "fin.ec_kind", r.kind) : (locale === "fi" ? "Palvelut" : "Services")) },
                { key: "amount", header: t("fin.col.amount"), align: "right", sort: (r) => first(r.amount_cents, r.services_cents, r.value_cents),
                  cell: (r) => eur(first(r.amount_cents, r.services_cents, r.value_cents), locale),
                  total: (x) => eur(first(x.amount_cents, x.services_cents, x.total_cents), locale) },
              ]} />
          )}
        </Loadable>
        <p className={cn("mt-2 text-xs text-ink-tertiary")}>{t("fin.vat.ec_note")}</p>
      </Section>
    </div>
  );
}
