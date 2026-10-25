import { aiErrorInfo } from "@/lib/aiDisplay";
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowLeft, ArrowRight, ChevronRight, Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState } from "@/components/app/states";
import { apiPatch, apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateShort } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import AiDraftCard from "./AiDraftCard";
import Composer from "./Composer";
import MessageItem from "./MessageItem";
import { draftingFresh, shipmentPath } from "./util";

/** Shipment context: reference, status, route, ETA, link to the shipment. */
export function ShipmentChip({ shipment, mode, className }) {
  const { t, locale } = useT();
  if (!shipment) return null;
  return (
    <Link to={shipmentPath(mode, shipment.id)} aria-label={`${t("messages.thread.open_shipment")} ${shipment.reference}`}
      className={cn("group flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 surface-flat rounded-lg px-2.5 py-1.5 text-xs hover:bg-hover", className)}>
      <span className="text-sm font-medium tabular">{shipment.reference}</span>
      <StatusBadge size="sm" group="status" value={shipment.status} />
      {shipment.has_exception && shipment.status !== "exception" && <StatusBadge size="sm" state="failed" label={t("messages.thread.exception")} />}
      <span className="min-w-0 truncate text-ink-secondary">
        {shipment.origin?.name || "—"} → {shipment.destination?.name || "—"}
      </span>
      <span className="tabular text-ink-secondary"
        title={t(shipment.status === "delivered" ? "messages.thread.delivered_title" : "messages.thread.eta_title")}>
        {shipment.status === "delivered"
          ? t("messages.thread.delivered", { date: shipment.delivered_at ? dateShort(shipment.delivered_at, locale) : "—" })
          : t("messages.thread.eta", { date: shipment.eta ? dateShort(shipment.eta, locale) : "—" })}
      </span>
      <ChevronRight className="ml-auto size-3.5 text-ink-tertiary group-hover:text-foreground" aria-hidden />
    </Link>
  );
}

function ThreadSkeleton() {
  return (
    <div className="flex flex-1 flex-col gap-4 p-4" aria-busy="true">
      <Skeleton className="h-5 w-1/2 rounded-md" />
      <Skeleton className="h-8 w-3/4 rounded-lg" />
      <Skeleton className="h-16 w-2/3 rounded-xl" />
      <Skeleton className="ml-auto h-16 w-2/3 rounded-xl" />
      <Skeleton className="h-12 w-1/2 rounded-xl" />
    </div>
  );
}

/** Ops-only AI row under the timeline: drafting busy state, needs_person note, or "Luonnostele AI:lla". */
function AiAssist({ thread, lastIsCustomer, lastCustomerAt, hasPending, canDraft, onDrafted }) {
  const { t } = useT();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null);
  const run = async () => {
    setBusy(true);
    setNote(null);
    try {
      const r = await apiPost(`/messages/threads/${thread.id}/ai-draft`, {});
      if (!r?.message) setNote(r?.ai_status === "needs_person" ? null : t("messages.ai.nothing_to_draft", { note: r?.ai_note || "—" }));
      await onDrafted?.();
    } catch (e) {
      setNote(t("messages.ai.failed", { error: e?.message || t("state.error") }));
    } finally {
      setBusy(false);
    }
  };
  if (hasPending) return null;
  const drafting = busy || (thread.ai_status === "drafting" && draftingFresh(lastCustomerAt));
  if (drafting) {
    return (
      <div className="flex items-center gap-2 self-end rounded-lg px-2 py-1.5 text-xs text-ink-secondary" aria-live="polite">
        <AgentMark />
        <Loader2 className="size-3.5 animate-spin" aria-hidden />
        <span>{t("messages.ai.drafting")}</span>
        <span className="text-ink-tertiary">{t("messages.ai.drafting_hint")}</span>
      </div>
    );
  }
  const needsPerson = thread.ai_status === "needs_person" && thread.needs_reply;
  if (!lastIsCustomer && !needsPerson && !note) return null;
  return (
    <div className="flex flex-col items-end gap-1.5 self-stretch">
      {needsPerson && (
        <p className="flex items-start gap-1.5 self-stretch rounded-lg bg-[var(--bg-subtle)] px-3 py-2 text-xs text-ink-secondary" role="status">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning-fg" aria-hidden />
          <span>{t("messages.ai.needs_person", { note: `${aiErrorInfo({error:thread.ai_note},t).reason} ${aiErrorInfo({error:thread.ai_note},t).next}` })}</span>
        </p>
      )}
      {note && <p className="text-xs text-ink-secondary" role="status">{note}</p>}
      {lastIsCustomer && canDraft && (
        <Button size="sm" variant="outline" onClick={run}>
          <Sparkles /> {t("messages.ai.draft_now")}
        </Button>
      )}
    </div>
  );
}

/**
 * One conversation: header (subject, shipment chip, close/reopen), timeline, AI draft card (ops), composer.
 * Polls every 10 s; composer text and scroll position survive the polls.
 * onChanged(kind) lets the list refresh ("read" | "posted" | "status" | "decided").
 */
export default function ThreadView({ threadId, mode = "customer", embedded = false, backTo, onChanged, headerExtra, className }) {
  const { t } = useT();
  const { can } = useAuth();
  const q = useApi(threadId ? `/messages/threads/${threadId}` : null, { pollMs: 10000 });
  const scrollRef = useRef(null);
  const lastCount = useRef({ id: null, n: 0 });
  const [statusBusy, setStatusBusy] = useState(false);
  const [statusError, setStatusError] = useState(null);
  const data = q.data && q.data.thread?.id === threadId ? q.data : null;
  const thread = data?.thread;
  const messages = data?.messages || [];
  const canOps = mode !== "ops" || can("messages.ops");

  // Mark read when opened, and again when a poll brings new unread messages.
  const unread = thread ? (mode === "ops" ? thread.unread_ops : thread.unread_customer) : 0;
  useEffect(() => {
    if (!thread || !unread) return;
    let cancelled = false;
    apiPost(`/messages/threads/${thread.id}/read`, {})
      .then(() => {
        if (cancelled) return;
        q.setData((d) => (d?.thread ? { ...d, thread: { ...d.thread, [mode === "ops" ? "unread_ops" : "unread_customer"]: 0 } } : d));
        onChanged?.("read", thread.id);
      })
      .catch(() => {});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [thread?.id, unread]);

  // Open at the first complete message; follow new messages only while the reader is near the end.
  const visibleCount = messages.length;
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || !data) return;
    const first = lastCount.current.id !== threadId;
    const grew = visibleCount > lastCount.current.n;
    const nearEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (first) el.scrollTop = 0;
    else if (grew && nearEnd) el.scrollTop = el.scrollHeight;
    lastCount.current = { id: threadId, n: visibleCount };
  }, [threadId, visibleCount, data]);

  const reload = useCallback(async () => { await q.reload(); }, [q]);

  const setStatus = async (status) => {
    setStatusBusy(true);
    setStatusError(null);
    try {
      const th = await apiPatch(`/messages/threads/${threadId}`, { status });
      q.setData((d) => (d ? { ...d, thread: { ...d.thread, ...th } } : d));
      onChanged?.("status", threadId);
    } catch (e) {
      setStatusError(t("messages.thread.status_failed", { error: e?.message || t("state.error") }));
    } finally {
      setStatusBusy(false);
    }
  };

  const post = async ({ body, attachments, visibility }) => {
    await apiPost(`/messages/threads/${threadId}/messages`, { body, attachments, visibility });
    await q.reload();
    onChanged?.("posted", threadId);
  };

  const onDecided = async () => {
    await q.reload();
    onChanged?.("decided", threadId);
  };

  if (!threadId) return null;
  if (!data && q.loading) return <ThreadSkeleton />;
  if (!data && q.error) {
    return (
      <div className="flex flex-1 flex-col gap-3 p-4">
        {backTo && (
          <Button asChild variant="ghost" size="sm" className="self-start md:hidden">
            <Link to={backTo}><ArrowLeft /> {t("messages.thread.back")}</Link>
          </Button>
        )}
        {q.error.status === 404 ? <EmptyState title={t("messages.thread.not_found")} /> : <ErrorState error={q.error} onRetry={reload} />}
      </div>
    );
  }
  if (!data) return <ThreadSkeleton />;

  const closed = thread.status === "closed";
  const pending = mode === "ops" ? messages.filter((m) => m.status === "pending_approval") : [];
  const sentVisible = messages.filter((m) => m.status === "sent" && m.visibility === "customer" && m.kind !== "update");
  const lastVisible = sentVisible[sentVisible.length - 1];
  const lastIsCustomer = lastVisible?.author?.type === "customer";
  const timeline = messages.filter((m) => m.status !== "pending_approval");
  const statusButton = (
    <Button size="sm" variant="outline" onClick={() => setStatus(closed ? "open" : "closed")} disabled={statusBusy}>
      {statusBusy && <Loader2 className="animate-spin" />}
      {closed ? t("messages.thread.reopen") : t("messages.thread.close")}
    </Button>
  );

  return (
    <div className={cn("flex min-h-0 flex-col", embedded ? "" : "h-full", className)} data-thread-id={threadId}>
      {!embedded ? (
        <div className="shrink-0 border-b border-[color:var(--border-subtle)] px-3 py-2.5 md:px-4">
          <div className="flex items-start gap-2">
            {backTo && (
              <Button asChild variant="ghost" size="icon-sm" className="-ml-1 mt-0.5 md:hidden">
                <Link to={backTo} aria-label={t("messages.thread.back")}><ArrowLeft /></Link>
              </Button>
            )}
            <div className="min-w-0 flex-1">
              <h2 className="truncate text-md font-medium">{thread.subject || thread.shipment_reference || "—"}</h2>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-tertiary">
                {mode === "ops" && thread.company_name && (
                  <Link to={`/ops/customers/${thread.company_id}`} className="text-ink-secondary hover:text-foreground hover:underline">{thread.company_name}</Link>
                )}
                {closed && <StatusBadge size="sm" state="expired" label={t("messages.thread.closed")} />}
                {q.error && <span className="text-danger-fg">{t("state.failed_refresh")}</span>}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">{statusButton}</div>
          </div>
          {data.shipment && <ShipmentChip shipment={data.shipment} mode={mode} className="mt-2" />}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2 border-b border-[color:var(--border-subtle)] px-3 py-2">
          {headerExtra}
          <span className="min-w-0 flex-1 truncate text-sm font-medium">{thread.subject || thread.shipment_reference}</span>
          {closed && <StatusBadge size="sm" state="expired" label={t("messages.thread.closed")} />}
          <Button asChild size="sm" variant="ghost">
            <Link to={`${mode === "ops" ? "/ops" : "/portal"}/messages/${threadId}`}>{t("messages.thread.open_in_inbox")} <ArrowRight /></Link>
          </Button>
          {statusButton}
        </div>
      )}
      {statusError && <ErrorState compact className="mx-3 mt-2" error={{ message: statusError }} />}
      {q.error && data && <ErrorState compact className="mx-3 mt-2" error={{ message: t("state.failed_refresh") }} onRetry={reload} />}

      <div ref={scrollRef} className={cn("min-h-0 overflow-y-auto px-3 py-4 md:px-4", embedded ? "max-h-[560px]" : "flex-1")}>
        <div className="flex flex-col gap-4">
          {timeline.length === 0 && pending.length === 0 && (
            <p className="text-sm text-ink-tertiary">{t("messages.thread.empty")}</p>
          )}
          {timeline.map((m) => <MessageItem key={m.id} m={m} mode={mode} />)}
          {pending.map((m) => <AiDraftCard key={m.id} m={m} onDecided={onDecided} />)}
          {mode === "ops" && (
            <AiAssist thread={thread} lastIsCustomer={lastIsCustomer} lastCustomerAt={lastIsCustomer ? lastVisible.sent_at || lastVisible.created_at : null} hasPending={pending.length > 0} canDraft={canOps} onDrafted={onDecided} />
          )}
        </div>
      </div>

      <div className="shrink-0 border-t border-[color:var(--border-subtle)] px-3 py-3 md:px-4">
        {closed && (
          <p className="mb-2 text-xs text-ink-tertiary">
            {mode === "ops" ? t("messages.thread.closed_note_ops") : t("messages.thread.closed_note_customer")}
          </p>
        )}
        <Composer key={threadId} mode={mode} draftKey={`thread:${threadId}`} onSend={post}
          target={thread.shipment_id ? { shipment_id: thread.shipment_id } : { company_id: thread.company_id }}
          disabledReason={canOps ? null : t("messages.no_permission")} rows={embedded ? 2 : 3} />
      </div>
    </div>
  );
}
