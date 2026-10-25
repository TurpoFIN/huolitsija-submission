import React from "react";
import { Link, useNavigate } from "react-router-dom";
import { ChevronRight, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader, Section, Metric } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { AccountBanners } from "@/components/app/VerifyBanner";
import ShipmentMap from "@/features/map/ShipmentMap";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, daysUntil, money } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const SEV = { danger: "bg-[var(--danger-500)]", warning: "bg-[var(--warning-500)]", info: "bg-[var(--info-500)]" };

export default function Overview() {
  const { t, locale } = useT();
  const { user } = useAuth();
  const navigate = useNavigate();
  const q = useApi("/portal/overview", { pollMs: 60000 });

  return (
    <div>
      <PageHeader
        title={t("overview.greeting", { name: (user?.name || "").split(" ")[0] })}
        meta={<Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />}
        actions={<Button asChild><Link to="/portal/quotes/new"><Plus />{t("overview.new_quote")}</Link></Button>}
      />
      <AccountBanners />
      <Loadable q={q} skeleton={<LoadingRows rows={8} />}>
        {(d) => (
          <>
            <div className="mb-5 flex flex-wrap gap-x-6 gap-y-2">
              <Metric value={d.counts.active} label={t("overview.active").toLowerCase()} to="/portal/shipments" />
              <Metric value={d.counts.in_transit} label={t("overview.in_transit")} to="/portal/shipments?status=in_transit" />
              <Metric value={d.counts.arriving_week} label={t("overview.arriving_week")} />
              <Metric value={d.counts.customs_open} label={t("overview.customs_open")} to="/portal/customs" />
              <Metric value={money(d.counts.open_invoices_cents, locale)} label={t("overview.open_amount")} to="/portal/invoices" />
            </div>
            <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
              <div className="min-w-0">
                <Section title={t("overview.actions")}>
                  {d.actions.length === 0 ? (
                    <p className="py-3 text-sm text-ink-tertiary">{t("overview.actions_none")}</p>
                  ) : (
                    <ul className="divide-y divide-[color:var(--border-subtle)]">
                      {d.actions.map((a, i) => {
                        const dd = a.due ? daysUntil(a.due) : null;
                        return (
                          <li key={i}>
                            <Link to={a.link} className="flex items-center gap-3 rounded-md px-1 py-2 hover:bg-hover">
                              <span className={cn("size-2 shrink-0 rounded-full", SEV[a.severity] || SEV.info)} aria-hidden />
                              <span className="w-40 shrink-0 text-sm font-medium truncate">{t(`action.kind.${a.kind}`)}</span>
                              <span className="min-w-0 flex-1 truncate text-sm text-ink-secondary">{[a.title, a.detail].filter(Boolean).join(" · ")}</span>
                              {a.amount_cents !== undefined && <span className="tabular text-sm">{money(a.amount_cents, locale)}</span>}
                              {dd !== null && <span className={cn("hidden sm:inline text-xs tabular", dd < 0 ? "text-danger-fg" : "text-ink-tertiary")}>{date(a.due, locale)}</span>}
                              <ChevronRight className="size-4 text-ink-tertiary" aria-hidden />
                            </Link>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </Section>
                <Section title={t("overview.active")} actions={<Button variant="ghost" size="xs" asChild><Link to="/portal/shipments">{t("action.view_all")}</Link></Button>}>
                  <DataTable
                    dense rows={d.active.slice(0, 12)}
                    onRowClick={(s) => navigate(`/portal/shipments/${s.id}`)}
                    empty={<EmptyState title={t("overview.active_none")} action={<Button size="sm" asChild><Link to="/portal/quotes/new">{t("overview.new_quote")}</Link></Button>} />}
                    columns={[
                      { key: "reference", header: t("field.reference"), cell: (s) => <span className="font-medium whitespace-nowrap">{s.reference}</span> },
                      { key: "route", header: `${t("field.origin")} → ${t("field.destination")}`, cell: (s) => <span className="block max-w-[180px] truncate text-ink-secondary" title={s.route}>{s.route}</span>, hideBelow: "md" },
                      { key: "mode", header: t("field.mode"), cell: (s) => tEnum(t, "mode", s.mode), hideBelow: "lg" },
                      { key: "status", header: t("field.status"), cell: (s) => <StatusBadge size="sm" value={s.status} /> },
                      { key: "eta", header: t("field.eta"), align: "right", cell: (s) => date(s.eta, locale) },
                    ]}
                  />
                </Section>
              </div>
              <div className="min-w-0">
                <Section title={t("overview.map")}>
                  <ShipmentMap scope="customer" height={420} initialView="europe" />
                </Section>
              </div>
            </div>
          </>
        )}
      </Loadable>
    </div>
  );
}
