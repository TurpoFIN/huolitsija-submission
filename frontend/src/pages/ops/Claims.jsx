// Ops claims queue. Decision: which submitted claims to review and decide before the time bar.
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Metric, PageHeader } from "@/components/app/layout";
import { FilterBar } from "@/components/app/FilterBar";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { SplitView, useMedia, useWide } from "@/features/exceptions/common";
import ClaimDetail from "@/features/claims/ClaimDetail";
import ClaimsList from "@/features/claims/ClaimsList";

const FILTERS = ["open", "draft", "approved", "rejected", "paid", "all"];

export default function Claims() {
  const { t, locale } = useT();
  const [params, setParams] = useSearchParams();
  const wide = useWide();
  const narrow = useMedia("(max-width: 767px)");
  const status = FILTERS.includes(params.get("status")) ? params.get("status") : "open";
  const selected = params.get("id");
  const [search, setSearch] = useState("");

  const setParam = useCallback((changes, replace = false) => {
    setParams((p) => {
      const n = new URLSearchParams(p);
      Object.entries(changes).forEach(([k, v]) => (v === null || v === undefined || v === "" ? n.delete(k) : n.set(k, v)));
      return n;
    }, { replace });
  }, [setParams]);

  const q = useApi(`/claims?status=${status}`, { pollMs: 30000 });
  const counts = q.data?.counts || {};
  const openCount = (counts.submitted || 0) + (counts.in_review || 0);

  const items = useMemo(() => {
    const list = q.data?.items || [];
    const s = search.trim().toLowerCase();
    if (!s) return list;
    return list.filter((c) => [c.number, c.company_name, c.shipment_reference].some((v) => (v || "").toLowerCase().includes(s)));
  }, [q.data, search]);

  useEffect(() => {
    if (wide && !selected && items.length) setParam({ id: items[0].id }, true);
  }, [wide, selected, items, setParam]);

  const countFor = (f) => (f === "all" ? Object.values(counts).reduce((a, b) => a + (b || 0), 0) : f === "open" ? openCount : counts[f] || 0);

  const meta = q.data ? (
    <>
      <Metric value={openCount} label={t("claims.meta.open")} to="/ops/claims" />
      <span className="text-ink-tertiary">·</span>
      <Metric value={money(q.data.open_claimed_cents || 0, locale, { decimals: 2 })} label={t("claims.meta.claimed")} />
      <span className="text-ink-tertiary">·</span>
      <Metric value={q.data.time_bar_alerts || 0} label={t("claims.meta.alerts")} tone={q.data.time_bar_alerts ? "danger" : undefined} />
    </>
  ) : null;

  const list = (
    <Loadable q={q} skeleton={<LoadingRows rows={6} />} isEmpty={() => items.length === 0}
      empty={<EmptyState title={t("claims.empty.ops")} body={status === "open" ? t("claims.empty.ops_body") : null} />}>
      {() => <ClaimsList rows={items} mode="ops" selected={selected} onOpen={(id) => setParam({ id })} narrow={narrow} />}
    </Loadable>
  );

  return (
    <div>
      <PageHeader title={t("claims.title")} meta={meta} actions={<Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />} />
      <FilterBar search={search} onSearch={setSearch} placeholder={t("claims.filter.search")}
        filters={[{ key: "status", label: t("claims.filter.status"), value: status, onChange: (v) => setParam({ status: v === "open" ? null : v }),
          options: FILTERS.map((f) => ({ value: f, label: t(`claims.filter.${f}`), count: q.data ? countFor(f) : undefined })) }]} />
      <SplitView wide={wide} open={!!selected} onClose={() => setParam({ id: null })} title={t("claims.title")} list={list}
        detail={selected
          ? <ClaimDetail key={selected} id={selected} mode="ops" onChanged={q.reload} onClose={wide ? null : () => setParam({ id: null })} />
          : <p className="p-4 text-sm text-ink-tertiary">{t("claims.select")}</p>} />
    </div>
  );
}
