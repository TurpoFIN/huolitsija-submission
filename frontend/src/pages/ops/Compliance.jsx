// Ops compliance (Vaatimustenmukaisuus). Decision first: what waits for a person (shipments on compliance hold,
// sanctions / dangerous goods reviews in the approval queue, open privacy requests), then one section at a time:
// party screenings, sanctions lists, dangerous goods, owners and PEP, privacy. Section in ?tab= so links and reloads keep it.
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Metric, PageHeader } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { useApi } from "@/lib/api";
import { dateShort } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { ApiFail } from "@/features/compliance/common";
import Screenings from "@/features/compliance/Screenings";
import Lists from "@/features/compliance/Lists";
import DangerousGoods from "@/features/compliance/DangerousGoods";
import Pep from "@/features/compliance/Pep";
import PrivacyOps from "@/features/compliance/PrivacyOps";

const TABS = { screenings: Screenings, lists: Lists, dg: DangerousGoods, pep: Pep, privacy: PrivacyOps };
// Approval kinds s12 puts in the owner queue (legal-human-points.md). Counted here, decided on /ops/approvals.
export const SANCTIONS_KINDS = ["sanctions_review", "kyc_review"];
export const DG_KINDS = ["dangerous_goods", "dg_review"];
const sum = (counts, kinds) => kinds.reduce((n, k) => n + (counts?.[k] || 0), 0);
const OPEN_PRIVACY = (r) => !["completed", "rejected", "expired"].includes(r.status);

function Waiting() {
  const { t, locale } = useT();
  const holds = useApi("/compliance/holds", { pollMs: 30000 });
  const appr = useApi("/approvals?status=pending", { pollMs: 30000 });
  const priv = useApi("/privacy/requests", { pollMs: 60000 });
  const v = (q, fn) => (q.data ? fn(q.data) : q.loading ? "…" : "—");
  const items = holds.data?.items || [];
  const counts = appr.data?.pending_counts;
  const privacyOpen = priv.data ? (priv.data.items || []).filter(OPEN_PRIVACY).length : null;
  const nothing = holds.data && appr.data && priv.data && !items.length && !sum(counts, SANCTIONS_KINDS) && !sum(counts, DG_KINDS) && !privacyOpen;
  const errors = [holds, appr, priv].filter((q) => q.error);
  return (
    <section aria-labelledby="cmp-waiting" className="mb-5 rounded-xl border border-[color:var(--border-subtle)] px-4 py-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 id="cmp-waiting" className="text-sm font-medium">{t("compliance.waiting")}</h2>
        <Button variant="ghost" size="xs" asChild><Link to="/ops/approvals">{t("compliance.waiting.open_approvals")} <ChevronRight className="size-3" aria-hidden /></Link></Button>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        <Metric value={v(holds, (d) => (d.items || []).length)} label={t("compliance.waiting.holds")} tone={items.length ? "danger" : undefined} to="/ops/shipments" />
        <Metric value={v(appr, (d) => sum(d.pending_counts, SANCTIONS_KINDS))} label={t("compliance.waiting.sanctions")} tone={sum(counts, SANCTIONS_KINDS) ? "warning" : undefined} to="/ops/approvals" />
        <Metric value={v(appr, (d) => sum(d.pending_counts, DG_KINDS))} label={t("compliance.waiting.dg")} tone={sum(counts, DG_KINDS) ? "warning" : undefined} to="/ops/approvals" />
        <Metric value={v(priv, () => privacyOpen)} label={t("compliance.waiting.privacy")} tone={privacyOpen ? "warning" : undefined} to="?tab=privacy" />
      </div>
      {nothing && <p className="mt-2 text-sm text-ink-tertiary">{t("compliance.waiting.none")}</p>}
      {items.length > 0 && (
        <ul className="mt-3 divide-y divide-[color:var(--border-subtle)] border-t border-[color:var(--border-subtle)]">
          {items.slice(0, 5).map((h) => (
            <li key={h.shipment_id}>
              <Link to={`/ops/shipments/${h.shipment_id}`} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 rounded-md px-1 py-2 hover:bg-hover">
                <span className="w-36 shrink-0 text-sm font-medium">{h.reference || h.shipment_id}</span>
                <span className="min-w-0 flex-1 basis-48 break-words text-sm text-ink-secondary">{h.reason || "—"}</span>
                {h.since && <span className="text-xs text-ink-tertiary">{t("compliance.holds.since", { time: dateShort(h.since, locale) })}</span>}
                <ChevronRight className="size-4 text-ink-tertiary" aria-hidden />
              </Link>
            </li>
          ))}
          {items.length > 5 && <li className="px-1 py-2 text-xs text-ink-tertiary">{t("compliance.holds.more", { n: items.length - 5 })}</li>}
        </ul>
      )}
      {errors.length > 0 && (
        <div className="mt-3 flex flex-col gap-1.5">
          {holds.error && <ApiFail compact what={t("compliance.waiting.holds_what")} error={holds.error} onRetry={holds.reload} />}
          {appr.error && <ApiFail compact what={t("nav.approvals")} error={appr.error} onRetry={appr.reload} />}
          {priv.error && <ApiFail compact what={t("compliance.priv.requests")} error={priv.error} onRetry={priv.reload} />}
        </div>
      )}
    </section>
  );
}

export default function OpsCompliance() {
  const { t } = useT();
  const [params, setParams] = useSearchParams();
  const tab = TABS[params.get("tab")] ? params.get("tab") : "screenings";
  const Body = TABS[tab];
  const setTab = (v) => {
    const n = new URLSearchParams(params);
    n.set("tab", v);
    setParams(n, { replace: true });
  };
  return (
    <div className="max-w-[1280px]">
      <PageHeader title={t("compliance.title")} />
      <Waiting />
      <div className="mb-4">
        <Segmented value={tab} onChange={setTab} label={t("compliance.tab.label")}
          options={Object.keys(TABS).map((k) => ({ value: k, label: t(`compliance.tab.${k}`) }))} />
      </div>
      <Body />
    </div>
  );
}
