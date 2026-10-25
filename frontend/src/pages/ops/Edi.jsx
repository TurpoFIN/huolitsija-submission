// Ops: EDI traffic. Decision: "is every partner's EDI flowing, and does anything need a person?"
// First line: gateway state (SFTP / AS2), messages in and out, problems. Then partners, then the message log; a message
// opens the raw interchange (one segment per line) and what we did with it.
import React, { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowDownLeft, ArrowUpRight, Cable, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { KV, Metric, PageHeader, Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { FilterBar } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

// Message status -> one of the nine workflow states (never a new meaning).
const MSG_STATE = { processed: "success", sent: "success", stored: "scheduled", queued: "in-progress", pending: "in-progress",
  retry: "pending", processed_with_errors: "pending", rejected: "failed", failed: "failed", dead: "failed" };
const PROBLEM = new Set(["processed_with_errors", "rejected", "failed", "dead", "retry"]);
const TYPES = ["IFTMIN", "IFTMCS", "IFTSTA", "DCSA_BOOKING", "DCSA_TNT"];

/** EDIFACT one segment per line; a released apostrophe (?') stays inside its segment. */
export function segments(raw) {
  if (!raw) return [];
  if (raw.includes("\n")) return raw.split(/\r?\n/).filter(Boolean);
  const out = [];
  let cur = "";
  for (let i = 0; i < raw.length; i += 1) {
    const c = raw[i];
    cur += c;
    if (c === "?" && i + 1 < raw.length) { cur += raw[i + 1]; i += 1; continue; }
    if (c === "'") { out.push(cur); cur = ""; }
  }
  if (cur.trim()) out.push(cur);
  return out;
}

function Direction({ d }) {
  const { t } = useT();
  const Icon = d === "in" ? ArrowDownLeft : ArrowUpRight;
  return (
    <span className="inline-flex items-center gap-1 whitespace-nowrap text-ink-secondary">
      <Icon className="size-3.5" aria-hidden />{t(`edi.dir.${d}`)}
    </span>
  );
}

function MsgStatus({ m }) {
  const { t } = useT();
  return <StatusBadge size="sm" state={MSG_STATE[m.status] || "expired"} value={m.status} label={t(`edi.status.${m.status}`) === `edi.status.${m.status}` ? m.status : t(`edi.status.${m.status}`)} />;
}

function Related({ m }) {
  const r = m.related || {};
  if (r.shipment_id) {
    return <Link to={`/ops/shipments/${r.shipment_id}`} onClick={(e) => e.stopPropagation()} className="whitespace-nowrap text-ink-accent hover:underline">{r.shipment_reference || "—"}</Link>;
  }
  if (r.quote_id) {
    return <Link to={`/ops/quotes/${r.quote_id}`} onClick={(e) => e.stopPropagation()} className="whitespace-nowrap text-ink-accent hover:underline">{(m.results || [])[0]?.quote_number || "—"}</Link>;
  }
  return <span className="text-ink-tertiary">—</span>;
}

function MessageSheet({ id, onClose, canReprocess, onDone }) {
  const { t, locale } = useT();
  const q = useApi(id ? `/edi/messages/${id}` : null, { enabled: !!id });
  const [busy, setBusy] = useState(false);
  const m = q.data;
  const reprocess = async () => {
    setBusy(true);
    try { await apiPost(`/edi/messages/${id}/reprocess`, {}); toast.success(t("edi.reprocessed")); onDone?.(); q.reload(); }
    catch (e) { toast.error(e.message); }
    finally { setBusy(false); }
  };
  const isJson = m?.standard === "dcsa";
  const answer = (m?.results || []).map((r) => ({ ...r }));
  const meta = m ? (({ raw, sha256, dedupe_key, ...rest }) => rest)(m) : null;
  return (
    <Sheet open={!!id} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-y-auto p-0 sm:max-w-2xl">
        <SheetHeader className="border-b border-[color:var(--border-subtle)] px-5 py-4 text-left">
          <SheetTitle className="flex flex-wrap items-center gap-2">
            {m ? <>{m.type} <span className="font-mono text-sm text-ink-tertiary">{m.interchange_ref || m.filename || ""}</span></> : t("edi.message")}
          </SheetTitle>
          <SheetDescription>{m ? `${t(`edi.dir.${m.direction}`)} · ${m.partner_name || m.sender || "—"} · ${dateTime(m.created_at, locale)}` : ""}</SheetDescription>
        </SheetHeader>
        <div className="px-5 py-4">
          {q.error && <ErrorState error={q.error} onRetry={q.reload} compact />}
          {!m && !q.error && <LoadingRows rows={6} />}
          {m && (
            <>
              <div className="mb-4 flex flex-wrap items-center gap-2">
                <MsgStatus m={m} />
                <span className="text-xs text-ink-tertiary">{t(`edi.transport.${m.transport}`) === `edi.transport.${m.transport}` ? m.transport : t(`edi.transport.${m.transport}`)}</span>
                {canReprocess && (m.direction === "out" ? PROBLEM.has(m.status) : m.standard === "edifact") && (
                  <Button size="xs" variant="outline" className="ml-auto" disabled={busy} onClick={reprocess}>
                    <RotateCcw /> {m.direction === "out" ? t("edi.resend") : t("edi.reprocess")}
                  </Button>
                )}
              </div>
              <KV className="mb-5" items={[
                { label: t("edi.f.sender"), value: m.sender, mono: true },
                { label: t("edi.f.recipient"), value: m.recipient, mono: true },
                { label: t("edi.f.related"), value: (m.related?.shipment_id || m.related?.quote_id) ? <Related m={m} /> : null },
                { label: t("edi.f.file"), value: m.filename || m.delivery?.path, mono: true },
                m.error && { label: t("edi.f.error"), value: <span className="text-danger-fg">{m.error}</span> },
              ]} />
              {answer.length > 0 && (
                <Section title={t("edi.answer")}>
                  <ul className="flex flex-col gap-2 text-sm">
                    {answer.map((r, i) => (
                      <li key={i} className="flex flex-col gap-0.5">
                        <span className="flex flex-wrap items-center gap-2">
                          <StatusBadge size="sm" state={r.status === "booked" || r.status === "cancelled" ? "success" : r.status === "rejected" ? "failed" : "in-review"}
                            value={r.status} label={t(`edi.result.${r.status}`) === `edi.result.${r.status}` ? r.status : t(`edi.result.${r.status}`)} />
                          <span className="font-mono text-[13px]">{r.doc_number}</span>
                          {r.function && <span className="text-xs text-ink-tertiary">{t("edi.function", { code: r.function })}</span>}
                          {r.shipment_reference && <Link to={`/ops/shipments/${r.shipment_id}`} className="text-ink-accent hover:underline">{r.shipment_reference}</Link>}
                        </span>
                        {(r.reasons || []).map((x, j) => <span key={j} className="text-xs text-ink-secondary">{x}</span>)}
                      </li>
                    ))}
                  </ul>
                </Section>
              )}
              <Section title={isJson ? t("edi.raw_json") : t("edi.raw_edifact")}>
                <pre className="max-h-[50vh] overflow-auto rounded-lg bg-secondary px-3 py-2 font-mono text-[12px] leading-5" data-testid="edi-raw">
                  {isJson ? m.raw : segments(m.raw).map((s, i) => (
                    <div key={i} className="whitespace-pre-wrap break-all"><span className="text-ink-tertiary select-none">{String(i + 1).padStart(2, " ")}  </span>{s}</div>
                  ))}
                </pre>
              </Section>
              <details className="text-sm">
                <summary className="cursor-pointer text-ink-secondary">{t("edi.record")}</summary>
                <pre className="mt-2 max-h-[40vh] overflow-auto rounded-lg bg-secondary px-3 py-2 font-mono text-[12px] leading-5">{JSON.stringify(meta, null, 2)}</pre>
              </details>
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

export default function Edi() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("settings.write");
  const status = useApi("/edi/status", { pollMs: 60000 });
  const partners = useApi("/edi/partners", { pollMs: 60000 });
  const [dir, setDir] = useState("all");
  const [type, setType] = useState("all");
  const [problemsOnly, setProblemsOnly] = useState(false);
  const qs = new URLSearchParams({ limit: "200" });
  if (dir !== "all") qs.set("direction", dir);
  if (type !== "all") qs.set("type", type);
  const msgs = useApi(`/edi/messages?${qs}`, { pollMs: 30000 });
  const [open, setOpen] = useState(null);

  const s = status.data;
  const problems = (msgs.data?.items || []).filter((m) => PROBLEM.has(m.status));
  const rows = problemsOnly ? problems : msgs.data?.items;

  const partnerCols = [
    { key: "name", header: t("edi.col.partner"), sort: (r) => r.name || "", cell: (r) => <span className="font-medium">{r.name || "—"}</span> },
    { key: "company_name", header: t("field.customer"), sort: (r) => r.company_name || "", className: "max-w-[14rem]",
      cell: (r) => (r.company_id ? <Link to={`/ops/customers/${r.company_id}`} className="block truncate hover:underline">{r.company_name || "—"}</Link> : <span className="text-ink-tertiary">—</span>) },
    { key: "transport", header: t("edi.col.transport"), sort: (r) => r.transport || "",
      cell: (r) => <span className="whitespace-nowrap">{t(`edi.transport.${r.transport}`) === `edi.transport.${r.transport}` ? r.transport : t(`edi.transport.${r.transport}`)}</span> },
    { key: "unb_id", header: t("edi.col.unb"), hideBelow: "md", sort: (r) => r.unb_id || "", cell: (r) => <span className="font-mono text-[13px]">{r.unb_id || "—"}</span> },
    { key: "formats", header: t("edi.col.formats"), hideBelow: "lg", sortable: false,
      cell: (r) => <span className="text-ink-secondary">{(r.formats || []).map((f) => t(`edi.format.${f}`) === `edi.format.${f}` ? f : t(`edi.format.${f}`)).join(", ") || "—"}</span> },
    { key: "last_message_at", header: t("edi.col.last"), sort: (r) => r.last_message_at || "", align: "right",
      cell: (r) => (r.last_message_at ? <span className="whitespace-nowrap tabular text-ink-secondary" title={dateTime(r.last_message_at, locale)}>{relTime(r.last_message_at, locale)} <span className="text-ink-tertiary">· {r.message_count}</span></span> : <span className="text-ink-tertiary">{t("edi.never")}</span>) },
    { key: "active", header: t("field.status"), sortable: false,
      cell: (r) => <StatusBadge size="sm" state={r.active === false ? "expired" : r.terms_accepted ? "success" : "pending"} value={r.active === false ? "inactive" : r.terms_accepted ? "active" : "no_terms"}
        label={t(r.active === false ? "edi.partner.inactive" : r.terms_accepted ? "edi.partner.active" : "edi.partner.no_terms")} /> },
  ];

  const msgCols = [
    { key: "created_at", header: t("edi.col.time"), sort: (r) => r.created_at, cell: (r) => <span className="whitespace-nowrap tabular text-ink-secondary">{dateTime(r.created_at, locale)}</span> },
    { key: "direction", header: t("edi.col.direction"), sort: (r) => r.direction, cell: (r) => <Direction d={r.direction} /> },
    { key: "type", header: t("edi.col.type"), sort: (r) => r.type, cell: (r) => <span className="font-mono text-[13px] whitespace-nowrap">{r.type}</span> },
    { key: "partner_name", header: t("edi.col.partner"), sort: (r) => r.partner_name || "", className: "max-w-[12rem]",
      cell: (r) => <span className="block truncate">{r.partner_name || r.sender || "—"}</span> },
    { key: "interchange_ref", header: t("edi.col.ref"), hideBelow: "lg", sort: (r) => r.interchange_ref || "",
      cell: (r) => <span className="font-mono text-[13px] text-ink-secondary">{r.interchange_ref || "—"}</span> },
    { key: "related", header: t("edi.col.related"), hideBelow: "md", sortable: false, cell: (r) => <Related m={r} /> },
    { key: "status", header: t("field.status"), sort: (r) => r.status, cell: (r) => <MsgStatus m={r} /> },
  ];

  const dirOptions = useMemo(() => ["all", "in", "out"].map((v) => ({ value: v, label: t(v === "all" ? "edi.all" : `edi.dir.${v}`) })), [t]);
  const typeOptions = useMemo(() => [{ value: "all", label: t("edi.all_types") }, ...TYPES.map((v) => ({ value: v, label: v.replace("DCSA_", "DCSA ") }))], [t]);

  return (
    <div>
      <PageHeader
        title={t("edi.title")}
        meta={s ? (
          <>
            <span className="inline-flex items-center gap-1.5">
              <StatusBadge size="sm" state={s.sftp_enabled ? "success" : "expired"} value="sftp" label={`SFTP ${t(s.sftp_enabled ? "edi.on" : "edi.off")}`} />
              <StatusBadge size="sm" state={s.as2_enabled && s.as2_ready ? "success" : s.as2_enabled ? "pending" : "expired"} value="as2"
                label={`AS2 ${t(s.as2_enabled ? (s.as2_ready ? "edi.on" : "edi.no_cert") : "edi.off")}`} />
            </span>
            <Metric value={s.partners} label={t("edi.metric.partners")} />
            <span aria-hidden className="text-ink-tertiary">·</span>
            <Metric value={s.messages?.in ?? 0} label={t("edi.metric.in")} />
            <span aria-hidden className="text-ink-tertiary">·</span>
            <Metric value={s.messages?.out ?? 0} label={t("edi.metric.out")} />
            {msgs.data && (
              <>
                <span aria-hidden className="text-ink-tertiary">·</span>
                <button type="button" onClick={() => setProblemsOnly((x) => !x)} className="rounded-md px-1 -mx-1 hover:bg-hover">
                  <Metric value={problems.length} tone={problems.length ? "warning" : undefined} label={t("edi.metric.problems")} />
                </button>
              </>
            )}
            <Freshness at={msgs.updatedAt} onRefresh={() => { status.reload(); partners.reload(); msgs.reload(); }} refreshing={msgs.refreshing} />
          </>
        ) : null}
      />
      {s && (
        <p className="mb-5 text-xs text-ink-tertiary">
          {t("edi.gateway_note", { unb: s.our_unb_id, n: s.poll_seconds })}{s.as2_id ? ` · AS2-ID ${s.as2_id}` : ""}
        </p>
      )}
      {status.error && <ErrorState className="mb-4" error={status.error} onRetry={status.reload} compact />}

      <Section title={<>{t("edi.partners")} {partners.data && <span className="tabular text-ink-tertiary">{partners.data.items.length}</span>}</>}>
        <Loadable q={partners} skeleton={<LoadingRows rows={4} />} isEmpty={(d) => !d.items.length}
          empty={<EmptyState icon={Cable} title={t("edi.partners_empty")} body={t("edi.partners_empty_body")} />}>
          {(d) => (
            <>
              <div className="hidden overflow-x-auto md:block"><DataTable rows={d.items} columns={partnerCols} dense pageSize={8} defaultSort={{ key: "last_message_at", dir: "desc" }} /></div>
              <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] md:hidden">
                {[...d.items].sort((a, b) => (b.last_message_at || "").localeCompare(a.last_message_at || "")).slice(0, 8).map((p) => (
                  <li key={p.id} className="flex items-center justify-between gap-3 py-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{p.name}</p>
                      <p className="truncate text-xs text-ink-tertiary">{p.company_name || "—"} · {t(`edi.transport.${p.transport}`)}</p>
                    </div>
                    <span className="shrink-0 text-xs text-ink-tertiary tabular">{p.last_message_at ? relTime(p.last_message_at, locale) : t("edi.never")}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Loadable>
      </Section>

      <Section title={t("edi.messages")}>
        <FilterBar filters={[
          { key: "dir", label: t("edi.col.direction"), value: dir, onChange: setDir, options: dirOptions },
          { key: "type", label: t("edi.col.type"), value: type, onChange: setType, options: typeOptions },
        ]} right={problemsOnly ? <Button size="xs" variant="ghost" onClick={() => setProblemsOnly(false)}>{t("edi.show_all")}</Button> : null} />
        <Loadable q={msgs} skeleton={<LoadingRows rows={8} />} isEmpty={() => !(rows || []).length}
          empty={<EmptyState icon={Cable} title={problemsOnly ? t("edi.no_problems") : t("edi.messages_empty")} body={problemsOnly ? null : t("edi.messages_empty_body")} />}>
          {() => (
            <>
              <div className="hidden overflow-x-auto md:block">
                <DataTable rows={rows} columns={msgCols} dense pageSize={25} onRowClick={(r) => setOpen(r.id)}
                  rowClassName={(r) => cn(PROBLEM.has(r.status) && "bg-[color:var(--status-pending-bg,transparent)]")} />
              </div>
              <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] md:hidden">
                {(rows || []).slice(0, 40).map((m) => (
                  <li key={m.id}>
                    <button type="button" onClick={() => setOpen(m.id)} className="flex w-full items-center justify-between gap-3 py-2 text-left">
                      <span className="min-w-0">
                        <span className="flex items-center gap-2 text-sm"><Direction d={m.direction} /><span className="font-mono text-[13px]">{m.type}</span></span>
                        <span className="block truncate text-xs text-ink-tertiary">{m.partner_name || m.sender || "—"} · {dateTime(m.created_at, locale)}</span>
                      </span>
                      <MsgStatus m={m} />
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Loadable>
      </Section>
      <MessageSheet id={open} onClose={() => setOpen(null)} canReprocess={canWrite} onDone={() => msgs.reload()} />
    </div>
  );
}
