import React from "react";
import { Link, useNavigate } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader, Section, Metric } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { Freshness, Loadable, LoadingRows } from "@/components/app/states";
import ShipmentMap from "@/features/map/ShipmentMap";
import { useApi } from "@/lib/api";
import { date, dateTime, money, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";

function ShipList({ rows, empty }) {
  const { t, locale } = useT();
  const navigate = useNavigate();
  if (!rows?.length) return <p className="py-2 text-sm text-ink-tertiary">{empty}</p>;
  return (
    <ul className="divide-y divide-[color:var(--border-subtle)]">
      {rows.map((s) => (
        <li key={s.id}>
          <button type="button" onClick={() => navigate(`/ops/shipments/${s.id}`)} className="flex w-full items-center gap-3 rounded-md px-1 py-2 text-left hover:bg-hover">
            <span className="w-32 shrink-0 text-sm font-medium">{s.reference}</span>
            <span className="min-w-0 flex-1 whitespace-normal break-words text-sm text-ink-secondary">{s.customer_name} · {s.route}</span>
            <StatusBadge size="sm" value={s.status} />
            <span className="hidden w-24 text-right text-xs tabular text-ink-tertiary sm:inline">{date(s.eta, locale)}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

export default function Today() {
  const { t, locale } = useT();
  const q = useApi("/ops/today", { pollMs: 30000 });
  return (
    <div>
      <PageHeader title={t("today.title")} meta={<Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />} />
      <Loadable q={q} skeleton={<LoadingRows rows={10} />}>
        {(d) => {
          const critical = (d.exceptions.counts.critical || 0) + (d.exceptions.counts.high || 0);
          const toReview = (d.customs.ready_for_review || 0) + (d.customs.approved || 0);
          return (
            <>
              <div className="mb-4 grid grid-cols-2 gap-x-5 gap-y-3 sm:grid-cols-3 xl:grid-cols-5">
                <Metric value={d.approvals.total} label={t("today.m.approvals")} to="/ops/approvals" tone={d.approvals.total ? "warning" : undefined} />
                <Metric value={d.exceptions.total} label={t("review.today.cases")} to="/ops/exceptions" tone={critical ? "danger" : undefined} />
                <Metric value={toReview} label={t("today.m.customs")} to="/ops/customs" />
                <Metric value={money(d.ar.overdue_cents, locale)} label={t("today.m.overdue", { n: d.ar.overdue_count })} to="/ops/billing" tone={d.ar.overdue_count ? "danger" : undefined} />
                <Metric value={d.kyc_review} label={t("today.m.kyc")} to="/ops/customers" />
              </div>
              <details className="mb-4 text-sm"><summary className="cursor-pointer text-ink-secondary">{t("review.today.more")}</summary><div className="mt-2 flex flex-wrap gap-5"><Metric value={d.shipments.active} label={t("today.active")} to="/ops/shipments" /><Metric value={`${d.ai.runs_24h} / ${d.ai.errors_24h}`} label={t("today.m.ai")} to="/ops/ai" /></div></details>
              <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <div className="min-w-0">
                  <Section title={t("today.approvals")} actions={<Button variant="ghost" size="xs" asChild><Link to="/ops/approvals">{t("action.view_all")}</Link></Button>}>
                    {d.approvals.oldest.length === 0 ? <p className="py-2 text-sm text-ink-tertiary">{t("today.approvals_none")}</p> : (
                      <ul className="divide-y divide-[color:var(--border-subtle)]">
                        {d.approvals.oldest.map((a) => (
                          <li key={a.id}>
                            <Link to={`/ops/approvals?focus=${a.id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-1 py-2 hover:bg-hover">
                              <span className="w-full text-xs font-medium text-ink-tertiary sm:w-40 sm:shrink-0 sm:text-sm">{t(`approvals.kind.${a.kind}`)}</span>
                              <span className="min-w-0 flex-1 whitespace-normal break-words text-sm text-ink-secondary">{a.title}</span>
                              {a.amount_cents ? <span className="tabular text-sm">{money(a.amount_cents, locale)}</span> : null}
                              <span className="text-xs text-ink-tertiary">{relTime(a.created_at, locale)}</span>
                              <ChevronRight className="size-4 text-ink-tertiary" aria-hidden />
                            </Link>
                          </li>
                        ))}
                      </ul>
                    )}
                  </Section>
                  <Section title={t("today.exceptions")} actions={<Button variant="ghost" size="xs" asChild><Link to="/ops/exceptions">{t("action.view_all")}</Link></Button>}>
                    {d.exceptions.top.length === 0 ? <p className="py-2 text-sm text-ink-tertiary">{t("today.exceptions_none")}</p> : (
                      <ul className="divide-y divide-[color:var(--border-subtle)]">
                        {d.exceptions.top.map((x) => (
                          <li key={x.id}>
                            <Link to={`/ops/exceptions?focus=${x.id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-1 py-2 hover:bg-hover">
                              <StatusBadge size="sm" group="severity" value={x.severity} />
                              <span className="order-3 basis-full min-w-0 flex-1 whitespace-normal break-words text-sm sm:order-none sm:basis-auto">{x.title}</span>
                              <span className="text-xs text-ink-tertiary">{dateTime(x.created_at, locale)}</span>
                            </Link>
                          </li>
                        ))}
                      </ul>
                    )}
                  </Section>
                  <Section title={t("today.no_carrier")}><ShipList rows={d.no_carrier} empty={t("today.none")} /></Section>
                  <Section title={t("today.holds")}><ShipList rows={d.holds} empty={t("today.none")} /></Section>
                </div>
                <div className="min-w-0">
                  <Section title={t("today.map")}><ShipmentMap scope="ops" height={420} initialView="europe" /><p className="mt-2 text-xs text-ink-tertiary">{t("review.today.map_hint")}</p></Section>
                  <Section title={t("today.arriving")}><ShipList rows={d.arriving_today} empty={t("today.none")} /></Section>
                  <Section title={t("today.customs")}>
                    <div className="flex flex-wrap gap-2">
                      {Object.entries(d.customs).length === 0 && <span className="text-sm text-ink-tertiary">{t("today.none")}</span>}
                      {Object.entries(d.customs).map(([k, v]) => (
                        <Link key={k} to={`/ops/customs?status=${k}`} className="inline-flex items-center gap-1.5 rounded-full hover:opacity-80">
                          <StatusBadge size="sm" group="customs" value={k} label={`${tEnum(t, "customs", k)} ${v}`} />
                        </Link>
                      ))}
                    </div>
                  </Section>
                </div>
              </div>
            </>
          );
        }}
      </Loadable>
    </div>
  );
}
