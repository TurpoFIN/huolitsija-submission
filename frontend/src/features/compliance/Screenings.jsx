// Party screenings: one row per screening of a shipment or customer company. Potential matches sort first; a row
// opens to every screened party (role, name, country, status) with its hits and the matcher's explanation.
import React, { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Section } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { EmptyState } from "@/components/app/states";
import { ScreeningHits } from "@/features/company/ScreeningHits";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { ApiFail, Block, Hairline, ScreenStatus, label } from "./common";

const STATUSES = ["potential_match", "error", "clear"];
const RANK = { potential_match: 0, error: 1, pending: 2, clear: 3 };
const SUBJECT_LINK = { shipment: (id) => `/ops/shipments/${id}`, company: (id) => `/ops/customers/${id}` };

function Party({ p }) {
  const { t } = useT();
  const hits = p.hits || [];
  return (
    <li className="flex flex-col gap-2 px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="w-40 shrink-0 text-xs font-medium text-ink-tertiary">{label(t, "compliance.role", p.role)}</span>
        <span className="min-w-0 flex-1 break-words text-sm">{p.name || "—"}</span>
        {p.country && <span className="font-mono text-xs text-ink-tertiary" title={t("compliance.scr.country")}>{p.country}</span>}
        <ScreenStatus value={p.status} />
      </div>
      {hits.length > 0 ? <ScreeningHits hits={hits} /> : p.status === "error" && p.reason ? <p className="text-xs text-danger-fg">{p.reason}</p> : null}
    </li>
  );
}

function Row({ s, open, onToggle }) {
  const { t, locale } = useT();
  const parties = s.parties || [];
  // hits a person already cleared as false positives are shown when the row opens, not counted as open hits
  const hitCount = parties.reduce((n, p) => n + (p.hits || []).filter((h) => !h.previously_cleared).length, 0);
  const to = SUBJECT_LINK[s.subject_type]?.(s.subject_id);
  const decided = s.decided && typeof s.decided === "object" ? s.decided : null;
  return (
    <li className={cn(open && "bg-[var(--bg-subtle)]", s.status === "potential_match" && "shadow-[inset_2px_0_0_var(--danger-500)]")}>
      <button type="button" onClick={onToggle} aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 text-left hover:bg-hover">
        {open ? <ChevronDown className="size-4 shrink-0 text-ink-tertiary" aria-hidden /> : <ChevronRight className="size-4 shrink-0 text-ink-tertiary" aria-hidden />}
        <span className="inline-flex min-w-0 flex-1 flex-col sm:flex-none sm:w-64">
          <span className="truncate text-sm font-medium">{s.subject_label || s.subject_id || "—"}</span>
          <span className="text-[11px] text-ink-tertiary">{label(t, "compliance.subject", s.subject_type)} · {label(t, "compliance.trigger", s.trigger)}</span>
        </span>
        <span className="hidden min-w-0 flex-1 text-sm text-ink-secondary sm:inline">
          {t("compliance.scr.parties_n", { n: parties.length })} · {hitCount ? t("compliance.scr.hits_n", { n: hitCount }) : t("compliance.scr.no_hits")}
        </span>
        <ScreenStatus value={s.status} />
        {decided && <ScreenStatus value={decided.decision || decided.status} />}
        <span className="w-24 text-right text-xs text-ink-tertiary" title={s.created_at}>{relTime(s.created_at, locale)}</span>
      </button>
      {open && (
        <div className="flex flex-col gap-2 px-3 pb-4 pt-1 sm:pl-10">
          {parties.length ? <Hairline>{parties.map((p, i) => <Party key={`${p.role}-${i}`} p={p} />)}</Hairline>
            : <p className="text-sm text-ink-tertiary">—</p>}
          <div className="flex flex-wrap items-center gap-3 text-xs text-ink-tertiary">
            <span>{t("compliance.scr.col.created")}: {dateTime(s.created_at, locale)}</span>
            {decided && <span>{t("compliance.scr.decided")}: {[decided.at && dateTime(decided.at, locale), decided.by?.name || decided.by, decided.note].filter(Boolean).join(" · ")}</span>}
            {to && <Button variant="link" size="xs" asChild className="px-0"><Link to={to}>{t("compliance.scr.open_subject")}</Link></Button>}
          </div>
        </div>
      )}
    </li>
  );
}

export default function Screenings() {
  const { t } = useT();
  const { can } = useAuth();
  const [status, setStatus] = useState(null);
  const [openId, setOpenId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const q = useApi(`/compliance/screenings?limit=50${status ? `&status=${status}` : ""}`, { pollMs: 60000 });
  const rows = useMemo(() => [...(q.data?.items || [])].sort((a, b) => (RANK[a.status] ?? 9) - (RANK[b.status] ?? 9)
    || String(b.created_at || "").localeCompare(String(a.created_at || ""))), [q.data]);
  const rescreen = async () => {
    setBusy(true);
    setErr(null);
    try {
      await apiPost("/compliance/rescreen-all", {});
      toast.success(t("compliance.scr.rescreen_started"));
      q.reload();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };
  const options = [{ value: null, label: t("compliance.scr.all") }, ...STATUSES.map((s) => ({ value: s, label: label(t, "compliance.status", s) }))];
  return (
    <Section title={t("compliance.tab.screenings")} actions={can("kyc.approve") && (
      <Button size="sm" variant="outline" disabled={busy} onClick={rescreen}>
        <RefreshCw className={busy ? "animate-spin" : undefined} /> {t("compliance.scr.rescreen")}
      </Button>
    )}>
      <div className="mb-3"><Segmented value={status} onChange={setStatus} label={t("compliance.scr.filter")} options={options} /></div>
      {err && <ApiFail className="mb-2" compact error={err} onRetry={rescreen} />}
      <Block q={q} rows={6} isEmpty={() => rows.length === 0}
        empty={<EmptyState title={status ? t("compliance.scr.empty_filter") : t("compliance.scr.empty")} body={status ? null : t("compliance.scr.empty_body")} />}>
        {() => (
          <Hairline>
            {rows.map((s) => <Row key={s.id} s={s} open={openId === s.id} onToggle={() => setOpenId(openId === s.id ? null : s.id)} />)}
          </Hairline>
        )}
      </Block>
    </Section>
  );
}
