// Ops quote work queue. Decision: which quotes need pricing or approval now.
import React, { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { StatusBadge, AgentMark } from "@/components/app/StatusBadge";
import { EmptyState, Freshness, Loadable } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { daysUntil, num, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Dash, MobileRow, ModeIcon, cargoText, eurTotal, isBlank, locCode, locName, useDebounced, useIsNarrow } from "@/features/quotes/ops/common";
import { approvalReasons } from "@/features/quotes/ops/quoteMath";
import { NewQuoteDialog } from "@/features/quotes/ops/NewQuoteDialog";
import { dateNum } from "@/features/quotes/ops/RatesTable";

const STATUSES = ["pending_approval", "draft", "sent", "accepted", "booked", "expired", "rejected"];
const SOURCES = ["portal", "public", "ai", "ops"];

function ValidUntil({ value, status }) {
  const { t, locale } = useT();
  if (!value) return <Dash />;
  const d = daysUntil(value);
  const closed = ["accepted", "booked", "rejected"].includes(status);
  return (
    <span className="whitespace-nowrap text-xs">
      <span className="tabular text-ink-tertiary">{t("oq.q.valid_short", { date: dateNum(value, locale) })}</span>
      {!closed && d !== null && d < 0 && <span className="ml-1 text-ink-tertiary">· {t("oq.q.lapsed")}</span>}
      {!closed && d !== null && d >= 0 && d <= 3 && <span className="ml-1 font-medium text-warning-fg">· {d === 0 ? t("oq.q.ends_today") : t("oq.q.days_left", { n: d })}</span>}
    </span>
  );
}

function marginOf(q) {
  const r = q.recommended_option || {};
  const cents = r.margin_cents ?? q.best_margin_cents ?? q.margin_cents ?? null;
  const pctV = r.margin_pct ?? q.best_margin_pct ?? q.margin_pct ?? null;
  return { cents, pct: pctV };
}

function Reasons({ q }) {
  const { t } = useT();
  const list = approvalReasons(q.approval);
  if (!list.length) return <Dash />;
  const pending = q.approval?.status === "pending" || q.status === "pending_approval";
  const text = list.map((r) => (t(`oq.reason.${r}`) === `oq.reason.${r}` ? r : t(`oq.reason.${r}`))).join(" · ");
  if (pending) return <span className="whitespace-nowrap text-sm">{text}</span>;
  const decided = q.approval?.status ? tEnum(t, "approval", q.approval.status).toLowerCase() : null;
  return <span className="whitespace-nowrap text-xs text-ink-tertiary">{text}{decided ? ` · ${decided}` : ""}</span>;
}

export default function Quotes() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const navigate = useNavigate();
  const narrow = useIsNarrow();
  const [params, setParams] = useSearchParams();
  const status = params.get("status"); // null = not chosen yet (auto), "all" = explicit all
  const source = params.get("source") || null;
  const [search, setSearch] = useState(params.get("q") || "");
  const q = useDebounced(search.trim(), 250);
  const [newOpen, setNewOpen] = useState(false);

  const setParam = (k, v) => {
    const next = new URLSearchParams(params);
    if (v === null || v === undefined || v === "") next.delete(k);
    else next.set(k, v);
    setParams(next, { replace: true });
  };

  const qs = new URLSearchParams();
  if (status && status !== "all") qs.set("status", status);
  if (source) qs.set("source", source);
  if (q) qs.set("q", q);
  const res = useApi(`/quotes${qs.toString() ? `?${qs}` : ""}`, { pollMs: 60000 });
  const counts = res.data?.counts || {};

  // First decision: what needs approval. Default to "pending approval" when there is any, else all.
  useEffect(() => {
    if (status === null && res.data) {
      const next = new URLSearchParams(params);
      next.set("status", (res.data.counts?.pending_approval || 0) > 0 ? "pending_approval" : "all");
      setParams(next, { replace: true });
    }
  }, [status, res.data, params, setParams]);

  const px = "px-2";
  const columns = [
    { key: "number", header: t("oq.q.number"), sort: (r) => r.number || "", className: `${px} pl-3`, headClassName: "pl-3",
      cell: (r) => (
        <span className="flex flex-col whitespace-nowrap">
          <span className="flex items-center gap-1.5"><span className="font-medium">{r.number || t("oq.q.no_number")}</span>{r.source === "ai" && <AgentMark />}</span>
          <span className="text-xs text-ink-tertiary">{t(`oq.q.src.${r.source}`)}</span>
        </span>
      ) },
    { key: "customer_name", header: t("field.customer"), sort: (r) => (r.customer_name || "").toLowerCase(), className: px, headClassName: px,
      cell: (r) => <span className="block max-w-[170px] truncate" title={r.customer_name || ""}>{r.customer_name || <span className="text-ink-tertiary">{t("oq.q.anonymous")}</span>}</span> },
    { key: "route", header: t("oq.q.route"), sort: (r) => `${locCode(r.origin)}${locCode(r.destination)}`, className: px, headClassName: px,
      cell: (r) => {
        const m = r.mode || r.recommended_option?.mode;
        return (
          <span className="flex flex-col" title={`${locName(r.origin) || "—"} → ${locName(r.destination) || "—"}`}>
            <span className="flex items-center gap-1.5 whitespace-nowrap font-mono text-xs">
              {m && <ModeIcon mode={m} className="size-3.5" />}
              {locCode(r.origin) || "—"} <span className="text-ink-tertiary">→</span> {locCode(r.destination) || "—"}
            </span>
            <span className="text-xs text-ink-tertiary">{m ? tEnum(t, "mode", m) : t("oq.q.all_modes")}</span>
          </span>
        );
      } },
    { key: "cargo_summary", header: t("oq.q.cargo"), sortable: false, className: px, headClassName: px,
      cell: (r) => (
        <span className="flex max-w-[210px] flex-col">
          <span className="truncate text-ink-secondary" title={r.cargo_summary || ""}>{(r.cargo && cargoText({ ...r.cargo, description: null }, t, locale)) || r.cargo_summary || "—"}</span>
          {(r.incoterm || r.dangerous_goods) && <span className="truncate text-xs text-ink-tertiary">{[r.incoterm && `${r.incoterm}${r.incoterm_place ? ` ${r.incoterm_place}` : ""}`, r.dangerous_goods && t("oq.q.dg")].filter(Boolean).join(" · ")}</span>}
        </span>
      ) },
    { key: "total", header: t("oq.q.total"), align: "right", sort: (r) => r.recommended_option?.total_cents ?? r.best_total_cents, className: px, headClassName: px,
      cell: (r) => {
        const v = r.recommended_option?.total_cents ?? r.best_total_cents;
        return isBlank(v) ? <span className="text-ink-tertiary">{t("oq.q.not_priced")}</span> : (
          <span className="flex flex-col items-end whitespace-nowrap">
            <span>{eurTotal(v, locale)}</span>
            {r.options_count > 1 && <span className="text-xs text-ink-tertiary">{t("oq.q.options_n", { n: r.options_count })}</span>}
          </span>
        );
      } },
    { key: "margin", header: t("oq.q.margin"), align: "right", sort: (r) => marginOf(r).pct, className: px, headClassName: px,
      cell: (r) => {
        const m = marginOf(r);
        if (isBlank(m.cents) && isBlank(m.pct)) return <Dash />;
        return (
          <span className="flex flex-col items-end whitespace-nowrap">
            <span>{isBlank(m.pct) ? "—" : `${num(m.pct, locale, 1)} %`}</span>
            {!isBlank(m.cents) && <span className="text-xs text-ink-tertiary">{eurTotal(m.cents, locale)}</span>}
          </span>
        );
      } },
    { key: "status", header: t("field.status"), sort: (r) => STATUSES.indexOf(r.status), className: px, headClassName: px,
      cell: (r) => (
        <span className="flex flex-col items-start gap-0.5">
          <StatusBadge size="sm" group="quote" value={r.status} />
          {approvalReasons(r.approval).length > 0 && <Reasons q={r} />}
        </span>
      ) },
    { key: "age", header: `${t("oq.q.age")} · ${t("oq.q.valid_until")}`, sort: (r) => r.created_at, className: `${px} pr-3`, headClassName: "pr-3",
      cell: (r) => (
        <span className="flex flex-col whitespace-nowrap">
          <span className="text-xs text-ink-secondary" title={r.created_at || ""}>{r.created_at ? relTime(r.created_at, locale) : "—"}</span>
          <ValidUntil value={r.valid_until} status={r.status} />
        </span>
      ) },
  ];

  const statusOptions = [
    { value: "all", label: t("oq.all"), count: res.data ? counts.all ?? res.data.total : undefined },
    ...STATUSES.map((s) => ({ value: s, label: tEnum(t, "quote", s), count: res.data ? counts[s] || 0 : undefined })),
  ];
  const filtered = (status && status !== "all") || source || q;

  return (
    <div>
      <PageHeader
        title={t("nav.quotes")}
        meta={res.data ? (
          <>
            {(counts.pending_approval || 0) > 0 ? (
              <button type="button" className="font-medium text-foreground hover:underline" onClick={() => setParam("status", "pending_approval")}>
                {t("oq.q.need_approval", { n: num(counts.pending_approval, locale) })}
              </button>
            ) : <span>{t("oq.q.none_pending")}</span>}
            {(counts.draft || 0) > 0 && (
              <button type="button" className="hover:underline" onClick={() => setParam("status", "draft")}>{t("oq.q.drafts_n", { n: num(counts.draft, locale) })}</button>
            )}
            <Freshness at={res.updatedAt} onRefresh={res.reload} refreshing={res.refreshing} />
          </>
        ) : null}
        actions={can("quotes.write") && <Button onClick={() => setNewOpen(true)}><Plus />{t("oq.q.new")}</Button>}
      />

      <FilterBar
        search={search}
        onSearch={(v) => { setSearch(v); setParam("q", v.trim()); }}
        placeholder={t("oq.q.search")}
        filters={[
          { key: "status", label: t("field.status"), value: status || "all", onChange: (v) => setParam("status", v), options: statusOptions },
          { key: "source", label: t("oq.q.source"), value: source, onChange: (v) => setParam("source", v),
            options: [{ value: null, label: t("oq.q.any_source") }, ...SOURCES.map((s) => ({ value: s, label: t(`oq.q.src.${s}`), count: res.data?.source_counts ? res.data.source_counts[s] || 0 : undefined }))] },
        ]}
      />

      <Loadable q={res} isEmpty={(d) => d.items.length === 0}
        empty={filtered ? (
          <EmptyState title={status === "pending_approval" ? t("oq.q.empty_pending") : t("oq.q.empty_filtered")}
            action={<Button size="sm" variant="outline" onClick={() => { setSearch(""); setParams(new URLSearchParams({ status: "all" }), { replace: true }); }}>{t("oq.q.show_all")}</Button>} />
        ) : (
          <EmptyState title={t("oq.q.empty")} body={t("oq.q.empty_body")} action={can("quotes.write") && <Button size="sm" onClick={() => setNewOpen(true)}><Plus />{t("oq.q.new")}</Button>} />
        )}>
        {(d) => (
          <>
            {res.refreshing && <p className="mb-1 text-xs text-ink-tertiary" aria-live="polite">{t("oq.updating")}</p>}
            {narrow ? (
              <ul className="divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
                {d.items.map((r) => {
                  const v = r.recommended_option?.total_cents ?? r.best_total_cents;
                  const reasons = approvalReasons(r.approval).map((x) => t(`oq.reason.${x}`)).join(", ");
                  return (
                    <MobileRow key={r.id} onClick={() => navigate(`/ops/quotes/${r.id}`)}
                      primary={r.number || t("oq.q.no_number")}
                      status={<StatusBadge size="sm" group="quote" value={r.status} />}
                      secondary={`${r.customer_name || t("oq.q.anonymous")} · ${locCode(r.origin) || "—"} → ${locCode(r.destination) || "—"}${reasons ? ` · ${reasons}` : ""}`}
                      right={isBlank(v) ? "—" : eurTotal(v, locale)}
                      rightSub={r.created_at ? relTime(r.created_at, locale) : null} />
                  );
                })}
              </ul>
            ) : (
              <div className="rounded-xl border border-[color:var(--border-subtle)]">
                <DataTable columns={columns} rows={d.items} dense className="min-w-[1080px]"
                  onRowClick={(r) => navigate(`/ops/quotes/${r.id}`)}
                  rowClassName={(r) => cn(["expired", "rejected"].includes(r.status) && "text-ink-secondary")} />
              </div>
            )}
            <p className="mt-2 text-xs text-ink-tertiary">{t("oq.q.shown", { n: num(d.items.length, locale), total: num(d.total ?? d.items.length, locale) })}</p>
          </>
        )}
      </Loadable>

      <NewQuoteDialog open={newOpen} onOpenChange={setNewOpen} onCreated={(qq) => navigate(`/ops/quotes/${qq.id}`)} />
    </div>
  );
}
