// Sanctions lists in use (EU, UN, OFAC SDN, UK, US CSL…): version, size, freshness, failure; refresh; ad-hoc name check.
// The name check is the existing POST /api/screening/check (routes/companies.py) via ScreeningCheck.
import React, { useState } from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ScreeningCheck } from "@/features/company/ScreeningHits";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, num, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { toast } from "sonner";
import { ApiFail, Block } from "./common";

const inUse = (l) => l.loaded !== false && l.status !== "error";

function ListStatus({ l }) {
  const { t } = useT();
  if (!inUse(l)) {
    return (
      <span className="inline-flex flex-col gap-0.5">
        <StatusBadge size="sm" state="failed" label={t("compliance.lists.not_loaded")} />
        {l.error && <span className="max-w-[280px] text-[11px] text-ink-tertiary break-words">{l.error}</span>}
      </span>
    );
  }
  if (l.status === "stale") return <StatusBadge size="sm" state="pending" label={t("compliance.lists.stale")} />;
  return <StatusBadge size="sm" state="success" label={t("compliance.lists.loaded")} />;
}

export default function Lists() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const q = useApi("/compliance/lists", { pollMs: 60000 });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const refresh = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await apiPost("/compliance/lists/refresh", {});
      if (r?.items) q.setData({ ...(q.data || {}), items: r.items });
      toast.success(t("compliance.lists.refresh_started"));
      q.reload();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };
  const columns = [
    { key: "label", header: t("compliance.lists.col.list"), sort: (l) => l.label || l.list,
      cell: (l) => (
        <span className="inline-flex min-w-0 flex-col">
          {l.source_url ? <a href={l.source_url} target="_blank" rel="noreferrer" className="font-medium hover:underline" onClick={(e) => e.stopPropagation()}>{l.label || l.list}</a>
            : <span className="font-medium">{l.label || l.list}</span>}
          <span className="text-[11px] text-ink-tertiary">{[l.short, l.authority].filter(Boolean).join(" · ") || l.list}</span>
        </span>
      ) },
    { key: "entries", header: t("compliance.lists.col.entries"), align: "right", sort: (l) => l.entries ?? -1,
      cell: (l) => (l.entries === null || l.entries === undefined ? <span className="text-ink-tertiary">—</span> : num(l.entries, locale)) },
    { key: "generated_at", header: t("compliance.lists.col.date"), sort: (l) => l.generated_at || "",
      cell: (l) => (l.generated_at ? <span className="tabular whitespace-nowrap">{date(l.generated_at, locale)}</span> : <span className="text-ink-tertiary">—</span>), hideBelow: "md" },
    { key: "fetched_at", header: t("compliance.lists.col.fetched"), sort: (l) => l.fetched_at || "",
      cell: (l) => (l.fetched_at ? <span className="whitespace-nowrap text-ink-secondary" title={l.fetched_at}>{relTime(l.fetched_at, locale)}</span> : <span className="text-ink-tertiary">—</span>) },
    { key: "status", header: t("compliance.lists.col.status"), sort: (l) => (inUse(l) ? 1 : 0), cell: (l) => <ListStatus l={l} /> },
  ];
  return (
    <>
      <Section title={t("compliance.tab.lists")} actions={can("kyc.approve") && (
        <Button size="sm" variant="outline" disabled={busy} onClick={refresh}>
          <RefreshCw className={busy ? "animate-spin" : undefined} /> {busy ? t("compliance.lists.refreshing") : t("compliance.lists.refresh")}
        </Button>
      )}>
        {err && <ApiFail className="mb-2" compact error={err} onRetry={refresh} />}
        <Block q={q} isEmpty={(d) => !d.items?.length} empty={<p className="py-2 text-sm text-ink-tertiary">{t("compliance.lists.empty")}</p>}>
          {(d) => {
            const missing = d.items.filter((l) => !inUse(l)).length;
            return (
              <>
                {missing > 0 && <p className="mb-2 text-sm text-warning-fg">{t("compliance.lists.incomplete", { n: missing, total: d.items.length })}</p>}
                <div className="overflow-x-auto"><DataTable dense rows={d.items} rowKey="list" columns={columns} /></div>
              </>
            );
          }}
        </Block>
      </Section>
      <Section title={t("compliance.lists.check")}>
        <p className="mb-2 text-xs text-ink-tertiary">{t("compliance.lists.check_hint")}</p>
        <ScreeningCheck />
      </Section>
    </>
  );
}
