// Exceptions of one shipment, embedded in the shipment detail pages.
// mode "ops": each row links to /ops/exceptions?id= and offers a quick resolve.
// mode "customer": read-only list with the resolution text.
import React, { useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, Loadable } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Age, DetectedBy, SlaDue, useNow } from "./common";
import ResolveDialog from "./ResolveDialog";
import { localDescription, localResolution, localTitle, typeLabel } from "./text";

export default function ShipmentExceptionsPanel({ shipment, mode = "customer", onChange }) {
  const { t, locale } = useT();
  const { can } = useAuth();
  const now = useNow();
  const ops = mode === "ops";
  const id = shipment?.id;
  const q = useApi(id ? `/exceptions?shipment_id=${id}&status=all` : null, { pollMs: ops ? 60000 : undefined });
  const [resolving, setResolving] = useState(null);
  const canWrite = ops && can("exceptions.write");

  if (!id) return null;
  return (
    <div>
      <Loadable q={q} isEmpty={(d) => d.items.length === 0}
        empty={<EmptyState title={ops ? t("exceptions.panel.empty") : t("exceptions.panel.empty_customer")} />}>
        {(d) => (
          <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
            {d.items.map((ex) => {
              const resolved = ex.status === "resolved";
              const desc = localDescription(ex, t, locale);
              return (
                <li key={ex.id} className={cn("flex flex-col gap-1.5 px-3 py-2.5", resolved && "text-ink-secondary")}>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <StatusBadge size="sm" group="severity" value={ex.severity} />
                    <StatusBadge size="sm" group="exception" value={ex.status} />
                    <span className="text-xs text-ink-secondary">{typeLabel(t, ex.type)}</span>
                    {ops && <DetectedBy value={ex.detected_by} />}
                    <span className="ml-auto text-xs text-ink-tertiary">
                      {ops && !resolved ? <SlaDue due={ex.sla_due} status={ex.status} now={now} /> : <time dateTime={ex.created_at}>{date(ex.created_at, locale)}</time>}
                    </span>
                  </div>
                  {ops ? (
                    <Link to={`/ops/exceptions?id=${ex.id}`} className="text-sm font-medium hover:underline">{localTitle(ex, t, locale)}</Link>
                  ) : (
                    <p className="text-sm font-medium">{localTitle(ex, t, locale)}</p>
                  )}
                  {!ops && desc && <p className="whitespace-pre-wrap text-sm text-ink-secondary">{desc}</p>}
                  {ops && (
                    <p className="text-xs text-ink-tertiary">
                      <Age at={ex.created_at} now={now} /> · {ex.owner?.name || t("exceptions.no_owner")}
                    </p>
                  )}
                  {resolved && ex.resolution && (
                    <p className="text-sm">
                      <span className="text-xs text-ink-tertiary">{t("exceptions.panel.resolution")} {ex.resolved_at ? `· ${dateTime(ex.resolved_at, locale)}` : ""}</span>
                      <span className="block whitespace-pre-wrap">{localResolution(ex.resolution, t)}</span>
                    </p>
                  )}
                  {ops && (
                    <div className="flex gap-2">
                      <Button size="xs" variant="ghost" asChild><Link to={`/ops/exceptions?id=${ex.id}`}>{t("exceptions.panel.open")}</Link></Button>
                      {canWrite && !resolved && (
                        <Button size="xs" variant="outline" onClick={() => setResolving(ex)}>{t("exceptions.panel.resolve")}</Button>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Loadable>
      {ops && (
        <ResolveDialog exception={resolving} open={!!resolving} onOpenChange={(o) => { if (!o) setResolving(null); }}
          onResolved={() => { q.reload(); onChange?.(); }} />
      )}
    </div>
  );
}
