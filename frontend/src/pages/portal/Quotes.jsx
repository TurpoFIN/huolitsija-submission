// Portal: quotes list. Decision: "which quote do I act on?" -> ready / expiring first, dense rows, status pills with counts.
import React, { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ChevronRight, Plus, Tags } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { date } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import ClaimNotice from "@/features/quotes/ClaimNotice";
import RouteText from "@/features/quotes/RouteText";
import { cargoSummary, eur, isExpired, modeIcon, validityDays, validityLabel } from "@/features/quotes/quoteFormat";

const MAIN = ["sent", "pending_approval", "accepted", "booked", "expired"];
// Default order answers "what do I act on": bookable first (soonest expiry first), then in review, then the rest by date.
const RANK = { accepted: 0, sent: 1, pending_approval: 2, draft: 3, expired: 4, booked: 5, rejected: 6 };
function actionOrder(items) {
  return [...items].sort((a, b) => {
    const ra = RANK[effectiveStatus(a)] ?? 9;
    const rb = RANK[effectiveStatus(b)] ?? 9;
    if (ra !== rb) return ra - rb;
    if (ra <= 1) return String(a.valid_until || "").localeCompare(String(b.valid_until || ""));
    return String(b.created_at || "").localeCompare(String(a.created_at || ""));
  });
}
const EXTRA = ["draft", "rejected"];

function effectiveStatus(r) {
  return r.status === "sent" && isExpired(r.valid_until) ? "expired" : r.status;
}

function Validity({ r, short = false }) {
  const { t } = useT();
  if (r.status === "booked" || r.status === "rejected") return <span className="text-ink-tertiary">—</span>;
  const d = validityDays(r.valid_until);
  const exp = d !== null && d < 0;
  return (
    <span className={cn("tabular whitespace-nowrap", exp ? "text-ink-tertiary" : d !== null && d <= 2 ? "text-warning-fg" : "text-ink-secondary")}>
      {short && d !== null && d > 0 && !exp ? t("pq.list.days_left", { n: d }) : validityLabel(t, r.valid_until)}
    </span>
  );
}

function Total({ r }) {
  const { t, locale } = useT();
  if (r.best_total_cents === null || r.best_total_cents === undefined) return <span className="text-ink-tertiary">—</span>;
  return (
    <span className="inline-flex flex-col items-end">
      <span className="tabular whitespace-nowrap">{eur(r.best_total_cents, locale)}</span>
      <span className="text-[11px] text-ink-tertiary whitespace-nowrap">{t("quotes.opt.excl_vat")}{r.incoterm ? ` · ${r.incoterm}` : ""}</span>
    </span>
  );
}

function Mode({ r }) {
  const { t } = useT();
  const m = r.mode || r.recommended_option?.mode;
  const I = modeIcon(m);
  return (
    <span className="inline-flex items-center gap-1.5 text-ink-secondary">
      <I className="size-4 text-ink-tertiary" aria-hidden />
      {r.mode ? tEnum(t, "mode", r.mode) : t("pq.list.any_mode")}
    </span>
  );
}

export default function Quotes() {
  const { t, locale } = useT();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || null;
  const [search, setSearch] = useState(params.get("q") || "");
  const [debounced, setDebounced] = useState(search);
  useEffect(() => {
    const h = setTimeout(() => setDebounced(search.trim()), 250);
    return () => clearTimeout(h);
  }, [search]);

  const qs = new URLSearchParams();
  if (status) qs.set("status", status);
  if (debounced) qs.set("q", debounced);
  const q = useApi(`/quotes${qs.toString() ? `?${qs}` : ""}`);
  const counts = useMemo(() => q.data?.counts || {}, [q.data]);

  const setStatus = (v) => {
    const next = new URLSearchParams(params);
    if (v) next.set("status", v);
    else next.delete("status");
    setParams(next, { replace: true });
  };

  const statusOptions = useMemo(() => {
    const opts = [{ value: null, label: t("pq.list.all"), count: counts.all ?? null }];
    MAIN.forEach((s) => opts.push({ value: s, label: tEnum(t, "quote", s), count: counts[s] ?? null }));
    EXTRA.forEach((s) => {
      if (counts[s] > 0 || status === s) opts.push({ value: s, label: tEnum(t, "quote", s), count: counts[s] ?? null });
    });
    return opts;
  }, [counts, status, t]);

  const ready = counts.sent ?? null;
  const columns = [
    { key: "number", header: t("pq.col.number"), sort: (r) => r.number || "",
      cell: (r) => (
        <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
          <span className="font-medium">{r.number || "—"}</span>
          {r.source === "ai" && <AgentMark />}
        </span>
      ) },
    { key: "route", header: t("pq.col.route"), sortable: false, className: "max-w-[22rem]",
      cell: (r) => <RouteText short origin={r.origin} destination={r.destination} /> },
    { key: "mode", header: t("field.mode"), hideBelow: "lg", sortable: false, cell: (r) => <span className="whitespace-nowrap"><Mode r={r} /></span> },
    { key: "cargo_summary", header: t("pq.col.cargo"), hideBelow: "lg", sortable: false,
      cell: (r) => <span className="tabular whitespace-nowrap text-ink-secondary">{r.cargo ? cargoSummary(r.cargo, t, locale) : r.cargo_summary || "—"}</span> },
    { key: "best_total_cents", header: t("pq.col.best_total"), align: "right", sort: (r) => r.best_total_cents, cell: (r) => <Total r={r} /> },
    { key: "status", header: t("field.status"), sort: (r) => r.status, cell: (r) => <StatusBadge group="quote" size="sm" value={effectiveStatus(r)} /> },
    { key: "valid_until", header: t("pq.col.validity"), sort: (r) => r.valid_until, cell: (r) => <Validity r={r} short /> },
    { key: "created_at", header: t("field.created"), align: "right", hideBelow: "md", sort: (r) => r.created_at,
      cell: (r) => <span className="text-ink-tertiary">{date(r.created_at, locale)}</span> },
  ];

  const filtered = !!(status || debounced);
  const empty = filtered ? (
    <EmptyState title={t("pq.list.none_match")} body={t("pq.list.none_match_body")}
      action={<Button size="sm" variant="outline" onClick={() => { setSearch(""); setStatus(null); }}>{t("pq.list.clear_filters")}</Button>} />
  ) : (
    <EmptyState icon={Tags} title={t("pq.list.empty")} body={t("pq.list.empty_body")}
      action={<Button size="sm" asChild><Link to="/portal/quotes/new"><Plus /> {t("pq.list.new")}</Link></Button>} />
  );

  return (
    <div>
      <PageHeader
        title={t("nav.quotes")}
        meta={
          <>
            {ready !== null && ready > 0 && (
              <button type="button" onClick={() => setStatus("sent")} className="rounded-md px-1 -mx-1 text-foreground hover:bg-hover">
                {t("pq.list.ready_n", { n: ready })}
              </button>
            )}
            {counts.pending_approval > 0 && <span>{t("pq.list.review_n", { n: counts.pending_approval })}</span>}
            <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
          </>
        }
        actions={<Button asChild><Link to="/portal/quotes/new"><Plus /> {t("pq.list.new")}</Link></Button>}
      />
      <ClaimNotice />
      <FilterBar
        search={search}
        onSearch={setSearch}
        placeholder={t("pq.list.search")}
        filters={[{ key: "status", label: t("field.status"), value: status, onChange: setStatus, options: statusOptions }]}
      />
      <Loadable q={q} skeleton={<LoadingRows rows={8} />} isEmpty={(d) => !d.items || d.items.length === 0} empty={empty}>
        {(d) => (
          <>
            <div className="hidden md:block">
              <DataTable rows={actionOrder(d.items)} columns={columns} dense onRowClick={(r) => navigate(`/portal/quotes/${r.id}`)}
                defaultSort={null} />
            </div>
            <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] md:hidden">
              {actionOrder(d.items).map((r) => (
                <li key={r.id}>
                  <Link to={`/portal/quotes/${r.id}`} className="flex items-center gap-3 rounded-md px-1 py-2.5 hover:bg-hover">
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="inline-flex items-center gap-1.5 text-sm font-medium">{r.number || "—"}{r.source === "ai" && <AgentMark />}</span>
                        <StatusBadge group="quote" size="sm" value={effectiveStatus(r)} />
                      </div>
                      <RouteText short origin={r.origin} destination={r.destination} className="text-sm" />
                      <div className="flex items-end justify-between gap-3 text-xs">
                        <span className="min-w-0 text-ink-tertiary">
                          <span className="block truncate tabular">{r.cargo ? cargoSummary(r.cargo, t, locale) : r.cargo_summary || "—"}</span>
                          {r.status !== "booked" && r.status !== "rejected" && <Validity r={r} />}
                        </span>
                        <span className="text-sm"><Total r={r} /></span>
                      </div>
                    </div>
                    <ChevronRight className="size-4 shrink-0 text-ink-tertiary" aria-hidden />
                  </Link>
                </li>
              ))}
            </ul>
            {d.total > d.items.length && (
              <p className="mt-2 text-xs text-ink-tertiary">{t("pq.list.showing", { n: d.items.length, total: d.total })}</p>
            )}
          </>
        )}
      </Loadable>
    </div>
  );
}
