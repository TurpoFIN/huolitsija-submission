// Luotto: every customer's credit position. Decision: who is on hold or over the limit, and whose check is stale.
import React, { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/app/states";
import { DataTable } from "@/components/app/DataTable";
import { Segmented } from "@/components/app/FilterBar";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { num, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { useIsNarrow } from "@/features/quotes/ops/common";
import { ComBadge, ComLoadable, Eur, NeedsLine, SourceLink, useLabel } from "./common";
import { CreditBody, useCreditRun } from "./CreditPanel";
import { creditPosition, listOf } from "./shapes";

const over = (p) => p.status === "blocked" || (p.available_cents !== null && p.available_cents < 0);
const attention = (p) => p.hold || over(p) || p.overdue_cents > 0 || ["watch", "hold", "blocked"].includes(p.status);

export default function CreditTab() {
  const { t, locale } = useT();
  const narrow = useIsNarrow();
  const q = useApi("/credit", { pollMs: 60000 });
  const provQ = useApi("/credit/providers");
  const [filter, setFilter] = useState(null);
  const [open, setOpen] = useState(null);
  const all = useMemo(() => listOf(q.data, "positions", "customers").map(creditPosition), [q.data]);
  const rows = useMemo(() => all.filter((p) => !filter || (filter === "attention" ? attention(p) : filter === "hold" ? p.hold : over(p)))
    .sort((a, b) => Number(attention(b)) - Number(attention(a)) || (a.available_cents ?? 0) - (b.available_cents ?? 0)), [all, filter]);
  const holds = all.filter((p) => p.hold).length;
  const overs = all.filter(over).length;
  const overdue = all.filter((p) => p.overdue_cents > 0).length;

  const columns = [
    { key: "name", header: t("field.customer"), sort: (p) => (p.name || "").toLowerCase(), cell: (p) => (
      <span className="flex min-w-0 max-w-[240px] flex-col"><span className="truncate font-medium">{p.name || "—"}</span><span className="font-mono text-xs text-ink-tertiary">{p.business_id || "—"}</span></span>
    ) },
    { key: "status", header: t("field.status"), sort: (p) => (p.hold ? 0 : 1), cell: (p) => <ComBadge group="credit" value={p.hold ? "hold" : p.status} /> },
    { key: "limit_cents", header: t("com.credit.limit"), align: "right", cell: (p) => <Eur cents={p.limit_cents} /> },
    { key: "open_ar_cents", header: t("com.credit.open_ar"), align: "right", cell: (p) => <Eur cents={p.open_ar_cents} /> },
    { key: "overdue_cents", header: t("com.credit.overdue"), align: "right", cell: (p) => <Eur cents={p.overdue_cents} className={p.overdue_cents > 0 ? "text-warning-fg" : undefined} /> },
    { key: "available_cents", header: t("com.credit.available"), align: "right", cell: (p) => <Eur cents={p.available_cents} className={over(p) ? "text-danger-fg font-medium" : undefined} /> },
    { key: "last", header: t("com.credit.latest"), hideBelow: "lg", sort: (p) => p.last_check?.at || "", cell: (p) => (
      <span className="flex flex-col text-xs">{p.last_check?.rating && <span>{p.last_check.rating}</span>}<span className="text-ink-tertiary">{p.last_check?.at ? relTime(p.last_check.at, locale) : t("com.credit.never_short")}</span></span>
    ) },
  ];

  return (
    <div>
      <NeedsLine items={[
        holds > 0 && { tone: "danger", icon: AlertTriangle, text: t("com.credit.holds_n", { n: num(holds, locale) }), onClick: () => setFilter("hold") },
        overs > 0 && { tone: "danger", text: t("com.credit.over_n", { n: num(overs, locale) }), onClick: () => setFilter("over") },
        overdue > 0 && { tone: "warning", text: t("com.credit.overdue_n", { n: num(overdue, locale) }) },
      ]} quiet={q.data ? t("com.credit.all_ok") : null} />
      {provQ.data && <Sources d={provQ.data} />}
      <div className="mb-3">
        <Segmented label={t("com.filter")} value={filter} onChange={setFilter} options={[
          { value: null, label: t("com.all"), count: all.length },
          { value: "attention", label: t("com.credit.f_attention"), count: all.filter(attention).length },
          { value: "hold", label: t("com.credit.f_hold"), count: holds },
          { value: "over", label: t("com.credit.f_over"), count: overs },
        ]} />
      </div>
      <ComLoadable q={q} perm="customers.write" isEmpty={() => rows.length === 0}
        empty={<EmptyState title={filter ? t("com.empty_filtered") : t("com.credit.empty")} />}>
        {() => (narrow ? (
          <ul className="divide-y divide-[color:var(--border-subtle)] border-y">
            {rows.map((p) => (
              <li key={p.company_id}>
                <button type="button" onClick={() => setOpen(p)} className="flex w-full flex-col gap-1 px-1 py-3 text-left hover:bg-hover">
                  <span className="flex items-start justify-between gap-3"><span className="min-w-0 truncate text-sm font-medium">{p.name || "—"}</span><ComBadge group="credit" value={p.hold ? "hold" : p.status} /></span>
                  <span className="flex flex-wrap gap-x-3 text-xs text-ink-secondary">
                    <span>{t("com.credit.available")} <Eur cents={p.available_cents} className={over(p) ? "text-danger-fg" : undefined} /></span>
                    <span>{t("com.credit.limit")} <Eur cents={p.limit_cents} /></span>
                    {p.overdue_cents > 0 && <span className="text-warning-fg">{t("com.credit.overdue")} <Eur cents={p.overdue_cents} /></span>}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : <div className="overflow-x-auto"><DataTable columns={columns} rows={rows} rowKey="company_id" dense onRowClick={setOpen} pageSize={50} /></div>)}
      </ComLoadable>
      <CreditSheet p={open} onClose={() => setOpen(null)} onChanged={q.reload} />
    </div>
  );
}

function Sources({ d }) {
  const { t } = useT();
  const label = useLabel();
  const b = d.bureau;
  return (
    <p className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-tertiary">
      <span>{t("com.credit.sources")}:</span>
      {(d.free || []).map((s) => <SourceLink key={s.name} href={s.url}>{label("csrc", s.name)}</SourceLink>)}
      <span>· {t("com.credit.bureau")}: {b ? `${label("csrc", b.name)}${b.mode ? ` (${label("provider", b.mode)})` : ""}${b.configured === false ? ` — ${t("com.credit.not_configured")}` : ""}` : t("com.credit.no_bureau_short")}</span>
    </p>
  );
}

function CreditSheet({ p, onClose, onChanged }) {
  const { t } = useT();
  const { can } = useAuth();
  const q = useApi(p ? `/credit/${p.company_id}` : null);
  const { run, busy } = useCreditRun(p?.company_id, () => { q.reload(); onChanged?.(); });
  return (
    <Sheet open={!!p} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-[520px]">
        <SheetHeader className="border-b border-[color:var(--border-subtle)] px-5 py-4 text-left">
          <SheetTitle className="pr-8 text-md font-medium">{p?.name || t("com.credit.panel")}</SheetTitle>
          <SheetDescription className="flex flex-wrap items-center gap-3 text-xs text-ink-tertiary">
            <span className="font-mono">{p?.business_id || "—"}</span>
            {p && <Link to={`/ops/customers/${p.company_id}`} className="text-ink-accent hover:underline">{t("com.credit.open_customer")}</Link>}
          </SheetDescription>
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {can("customers.write") && (
            <Button size="sm" variant="outline" className="mb-4" onClick={run} disabled={busy || !p}><RefreshCw className={busy ? "animate-spin" : undefined} />{busy ? t("com.credit.checking") : t("com.credit.run")}</Button>
          )}
          <ComLoadable q={q} perm="customers.write">{(d) => <CreditBody data={{ name: p?.name, ...d }} onChanged={() => { q.reload(); onChanged?.(); }} />}</ComLoadable>
        </div>
      </SheetContent>
    </Sheet>
  );
}
