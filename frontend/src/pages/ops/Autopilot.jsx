import React, { useEffect, useMemo, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PageHeader } from "@/components/app/layout";
import { ErrorState, Freshness, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { StateBar } from "@/features/autopilot/StateBar";
import { OwnerNow } from "@/features/autopilot/OwnerNow";
import { DecisionLog } from "@/features/autopilot/DecisionLog";
import { Guardrails } from "@/features/autopilot/Guardrails";
import { JobQueue } from "@/features/autopilot/JobQueue";
import { Monitoring } from "@/features/autopilot/Monitoring";
import { LegalPoints } from "@/features/autopilot/LegalPoints";

const TABS = ["decisions", "limits", "jobs", "monitor", "legal"];

/** /ops/autopilot: the control room for running the company on Autopilot. Decision first: is it acting, what needs
 *  the owner now; then the decision log, the guardrails, the job queue, monitoring and the legal human points. */
export default function Autopilot() {
  const { t } = useT();
  const [params, setParams] = useSearchParams();
  const tab = TABS.includes(params.get("tab")) ? params.get("tab") : "decisions";
  const status = useApi("/autopilot/status", { pollMs: 15000 });
  const legal = useApi("/autopilot/legal-points");
  const legalByCode = useMemo(() => Object.fromEntries((legal.data?.items || []).map((p) => [p.code, p])), [legal.data]);
  const scrollTo = useRef(null);

  const setTab = (next) => setParams((p) => { const n = new URLSearchParams(p); n.set("tab", next); return n; }, { replace: true });
  const openTab = (next, anchor) => { scrollTo.current = anchor || null; setTab(next); };
  useEffect(() => {
    if (!scrollTo.current) return undefined;
    const id = scrollTo.current;
    const timer = setTimeout(() => { document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }); scrollTo.current = null; }, 60);
    return () => clearTimeout(timer);
  }, [tab]);

  const d = status.data;
  const reload = status.reload;
  return (
    <div className="min-w-0">
      <PageHeader title={t("ap.title")} meta={<Freshness at={status.updatedAt} onRefresh={reload} refreshing={status.refreshing} />} />
      {status.error && d && <ErrorState className="mb-3" compact error={{ message: t("state.failed_refresh") }} onRetry={reload} />}
      {!d && status.loading && <LoadingRows rows={5} />}
      {!d && status.error && <ErrorState error={status.error} onRetry={reload} />}
      {d && (
        <>
          <StateBar status={d} onChanged={reload} />
          <OwnerNow status={d} openTab={openTab} />
          <Tabs value={tab} onValueChange={setTab} className="mt-2">
            <div className="-mx-1 overflow-x-auto px-1 no-scrollbar">
              <TabsList aria-label={t("ap.tabs")}>
                {TABS.map((k) => <TabsTrigger key={k} value={k}>{t(`ap.tab.${k}`)}</TabsTrigger>)}
              </TabsList>
            </div>
            <TabsContent value="decisions"><DecisionLog legal={legalByCode} /></TabsContent>
            <TabsContent value="limits"><Guardrails status={d} onChanged={reload} /></TabsContent>
            <TabsContent value="jobs"><JobQueue status={d} onChanged={reload} /></TabsContent>
            <TabsContent value="monitor"><Monitoring status={d} onChanged={reload} /></TabsContent>
            <TabsContent value="legal"><LegalPoints q={legal} /></TabsContent>
          </Tabs>
        </>
      )}
    </div>
  );
}
