// Ops customer list. Decision: which customers need a KYC / sanctions decision, and who cannot ship or clear yet.
import React, { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { PageHeader, Metric } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, Freshness, Loadable } from "@/components/app/states";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { money, num, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { DemoTag, PoaBadge, SanctionsBadge, useDebounced, useIsNarrow } from "@/features/company/common";

const KYC_FILTERS = ["in_review", "not_started", "pending", "approved", "rejected"];
const needsDecision = (c) => c.kyc_review_pending || c.kyc_status === "in_review" || c.sanctions_status === "potential_match";

function CustomerName({ c, withCity }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2">
        <span className="truncate font-medium">{c.name}</span>
        {c.demo && <DemoTag />}
      </div>
      <span className="text-xs text-ink-tertiary">
        <span className="font-mono">{c.business_id || "—"}</span>
        {withCity && c.city ? ` · ${c.city}` : ""}
      </span>
    </div>
  );
}

function MobileList({ rows, onOpen }) {
  const { t, locale } = useT();
  return (
    <ul className="divide-y divide-[color:var(--border-subtle)] border-y">
      {rows.map((c) => (
        <li key={c.id}>
          <button type="button" onClick={() => onOpen(c)} className="flex w-full flex-col gap-1 px-1 py-3 text-left hover:bg-hover">
            <div className="flex w-full items-start justify-between gap-3">
              <CustomerName c={c} />
              <StatusBadge group="kyc" value={c.kyc_status} size="sm" />
            </div>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-secondary">
              {c.sanctions_status !== "clear" && <SanctionsBadge value={c.sanctions_status} />}
              <PoaBadge valid={c.poa_valid} validTo={c.poa_valid_to} />
              <span>{c.city || "—"}</span>
              <span>· {t("customers.shipments_n", { n: num(c.shipments_count, locale) })}</span>
              <span className="tabular">· {money(c.credit_limit_cents, locale)}</span>
            </div>
          </button>
        </li>
      ))}
    </ul>
  );
}

export default function Customers() {
  const { t, locale } = useT();
  const navigate = useNavigate();
  const narrow = useIsNarrow();
  const [params, setParams] = useSearchParams();
  const kyc = params.get("kyc") || null;
  const sanctions = params.get("sanctions") || null;
  const [search, setSearch] = useState(params.get("q") || "");
  const q = useDebounced(search.trim(), 250);

  const setParam = (k, v) => {
    const next = new URLSearchParams(params);
    if (!v) next.delete(k);
    else next.set(k, v);
    setParams(next, { replace: true });
  };
  const qs = new URLSearchParams();
  if (q) qs.set("q", q);
  if (kyc) qs.set("kyc", kyc);
  if (sanctions) qs.set("sanctions", sanctions);
  const filtered = !!(q || kyc || sanctions);
  const res = useApi(`/ops/customers${qs.toString() ? `?${qs}` : ""}`, { pollMs: 60000 });
  // Unfiltered list for the header counts and filter pills, so they do not move with the filters.
  const allQ = useApi("/ops/customers", { enabled: filtered, pollMs: 60000 });
  const all = filtered ? allQ : res;

  const rows = useMemo(() => {
    const items = res.data?.items || [];
    return [...items].sort((a, b) => Number(needsDecision(b)) - Number(needsDecision(a)) || a.name.localeCompare(b.name, locale));
  }, [res.data, locale]);

  const summary = useMemo(() => {
    const items = all.data?.items || [];
    return {
      review: items.filter(needsDecision).length,
      matches: items.filter((c) => c.sanctions_status === "potential_match").length,
      noPoa: items.filter((c) => !c.poa_valid).length,
      total: all.data?.total ?? items.length,
    };
  }, [all.data]);

  const counts = all.data?.counts || {};
  const sCounts = all.data?.sanctions_counts || {};
  const kycOptions = [
    { value: null, label: t("customers.filter.all"), count: all.data?.total },
    ...KYC_FILTERS.filter((k) => k !== "pending" || counts.pending || kyc === "pending").map((k) => ({
      value: k, label: k === "in_review" ? t("customers.filter.review") : tEnum(t, "kyc", k), count: counts[k] || 0,
    })),
  ];
  const sanctionOptions = [
    { value: null, label: t("customers.filter.any") },
    { value: "potential_match", label: tEnum(t, "sanctions", "potential_match"), count: sCounts.potential_match || 0 },
    { value: "blocked", label: tEnum(t, "sanctions", "blocked"), count: sCounts.blocked || 0 },
  ];
  const open = (c) => navigate(`/ops/customers/${c.id}`);

  const columns = [
    { key: "name", header: t("customers.col.customer"), cell: (c) => <CustomerName c={c} withCity />, sort: (c) => c.name.toLowerCase() },
    { key: "kyc_status", header: t("customers.col.kyc"), cell: (c) => (
      <div className="flex flex-col items-start gap-0.5">
        <StatusBadge group="kyc" value={c.kyc_status} size="sm" />
        {c.kyc_review_pending && c.kyc_status !== "in_review" && <span className="text-[11px] text-warning-fg">{t("customers.review_pending")}</span>}
      </div>
    ) },
    { key: "sanctions_status", header: t("customers.col.sanctions"), cell: (c) => <SanctionsBadge value={c.sanctions_status} quietClear /> },
    { key: "poa", header: t("customers.col.poa"), cell: (c) => <PoaBadge valid={c.poa_valid} validTo={c.poa_valid_to} />, sort: (c) => (c.poa_valid ? c.poa_valid_to : "") },
    { key: "eori", header: t("customers.col.eori"), hideBelow: "lg", cell: (c) => (c.eori ? <span className="whitespace-nowrap font-mono text-xs">{c.eori}</span> : <span className="text-ink-tertiary">—</span>) },
    { key: "representation_default", header: t("customers.col.representation"), className: "hidden 2xl:table-cell", headClassName: "hidden 2xl:table-cell",
      cell: (c) => (c.representation_default ? tEnum(t, "representation", c.representation_default) : <span className="text-ink-tertiary">—</span>) },
    { key: "credit_limit_cents", header: t("customers.col.credit"), align: "right", cell: (c) => money(c.credit_limit_cents, locale) },
    { key: "payment_terms_days", header: t("customers.col.terms"), align: "right", hideBelow: "lg",
      cell: (c) => (c.payment_terms_days === null || c.payment_terms_days === undefined ? "—" : t("customers.days", { n: c.payment_terms_days })) },
    { key: "shipments_count", header: t("customers.col.shipments"), align: "right", cell: (c) => num(c.shipments_count, locale) },
    { key: "open_declarations", header: t("customers.col.open_customs"), align: "right", hideBelow: "lg", headClassName: "whitespace-nowrap", cell: (c) => num(c.open_declarations, locale) },
    { key: "kyc_checked_at", header: t("customers.col.checked"), className: "hidden 2xl:table-cell", headClassName: "hidden 2xl:table-cell",
      cell: (c) => <span className="text-xs text-ink-tertiary">{c.kyc_checked_at ? relTime(c.kyc_checked_at, locale) : "—"}</span> },
  ];

  return (
    <div className="max-w-[1280px]">
      <PageHeader
        title={t("customers.title")}
        meta={all.data && <>
          <Metric value={num(summary.review, locale)} label={t("customers.metric.review")} tone={summary.review ? "warning" : undefined}
            to={summary.review ? "/ops/customers?kyc=in_review" : undefined} />
          {summary.matches > 0 && <Metric value={num(summary.matches, locale)} label={t("customers.metric.matches")} tone="danger" to="/ops/customers?sanctions=potential_match" />}
          <Metric value={num(summary.noPoa, locale)} label={t("customers.metric.no_poa")} />
          <Metric value={num(summary.total, locale)} label={t("customers.metric.total")} />
        </>}
        actions={<Freshness at={res.updatedAt} onRefresh={() => { res.reload(); if (filtered) allQ.reload(); }} refreshing={res.refreshing} />}
      />
      <FilterBar search={search} onSearch={(v) => { setSearch(v); setParam("q", v.trim()); }} placeholder={t("customers.search")}
        filters={[
          { key: "kyc", label: t("customers.filter.kyc"), value: kyc, onChange: (v) => setParam("kyc", v), options: kycOptions },
          { key: "sanctions", label: t("customers.filter.sanctions"), value: sanctions, onChange: (v) => setParam("sanctions", v), options: sanctionOptions },
        ]} />
      <Loadable q={res} isEmpty={(d) => !d.items?.length}
        empty={filtered ? (
          <EmptyState title={t("customers.empty_filtered")}
            action={<Button size="sm" variant="outline" onClick={() => { setSearch(""); setParams(new URLSearchParams(), { replace: true }); }}>{t("customers.clear_filters")}</Button>} />
        ) : (
          <EmptyState title={t("customers.empty")} body={t("customers.empty_body")} />
        )}>
        {() => (narrow ? <MobileList rows={rows} onOpen={open} /> : (
          <div className="overflow-x-auto">
            <DataTable columns={columns} rows={rows} onRowClick={open} dense />
          </div>
        ))}
      </Loadable>
    </div>
  );
}
