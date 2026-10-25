import React, { useState } from "react";
import { MessageSquare } from "lucide-react";
import { AgentMark } from "@/components/app/StatusBadge";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import Composer from "./Composer";
import ThreadView from "./ThreadView";
import { compactTime } from "./util";

/**
 * The conversation(s) about one shipment, embedded in shipment detail. Same ThreadView + Composer as the inbox.
 * When none exists, the composer starts one for the shipment. mode decides the ops features; onChange after posting.
 */
export default function ShipmentMessagesPanel({ shipment, mode = "customer", onChange }) {
  const { t, locale } = useT();
  const { can } = useAuth();
  const sid = shipment?.id;
  const q = useApi(sid ? `/messages/threads?filter=all&shipment_id=${encodeURIComponent(sid)}` : null, { pollMs: 30000 });
  const [selected, setSelected] = useState(null);
  const items = q.data?.items || [];
  const current = items.find((x) => x.id === selected)?.id || items[0]?.id || null;
  const canPost = mode !== "ops" || can("messages.ops");

  const changed = (kind) => {
    q.reload();
    if (kind !== "read") onChange?.(kind);
  };

  const start = async ({ body, attachments }) => {
    const r = await apiPost("/messages/threads", {
      shipment_id: sid, body, attachments, ...(mode === "ops" ? { company_id: shipment.company_id || null } : {}),
    });
    if (r?.thread?.id) setSelected(r.thread.id);
    await q.reload();
    onChange?.("created");
  };

  if (!sid) return null;
  if (q.loading && !q.data) return <LoadingRows rows={4} />;
  if (q.error && !q.data) return <ErrorState error={q.error} onRetry={q.reload} />;

  if (items.length === 0) {
    return (
      <div className="flex flex-col gap-3 surface-flat rounded-xl p-3" data-panel="messages">
        <div className="flex items-start gap-2">
          <MessageSquare className="mt-0.5 size-4 shrink-0 text-ink-tertiary" aria-hidden />
          <div>
            <p className="text-sm font-medium">{t("messages.panel.empty")}</p>
            <p className="text-sm text-ink-tertiary">{mode === "ops" ? t("messages.panel.empty_body_ops") : t("messages.panel.empty_body_customer")}</p>
          </div>
        </div>
        <Composer mode="customer" draftKey={`shipment:${sid}`} onSend={start} rows={3}
          target={{ shipment_id: sid }} disabledReason={canPost ? null : t("messages.no_permission")}
          placeholder={mode === "ops" ? t("messages.composer.placeholder_ops") : undefined} />
      </div>
    );
  }

  const picker = items.length > 1 ? (
    <div role="tablist" aria-label={t("messages.panel.threads")} className="flex w-full flex-wrap gap-1.5">
      {items.map((it) => {
        const unread = (mode === "ops" ? it.unread_ops : it.unread_customer) > 0;
        return (
          <button key={it.id} type="button" role="tab" aria-selected={it.id === current} onClick={() => setSelected(it.id)}
            className={cn("inline-flex h-7 max-w-[260px] items-center gap-1.5 rounded-full px-2.5 text-xs font-medium text-ink-secondary hover:bg-hover",
              it.id === current && "bg-secondary text-foreground shadow-[inset_0_0_0_0.5px_var(--border-default)]")}>
            {unread && <span className="size-1.5 shrink-0 rounded-full bg-brand" aria-hidden />}
            <span className="truncate">{it.subject || it.shipment_reference}</span>
            <span className="shrink-0 tabular text-ink-tertiary">{compactTime(it.last_message_at, locale, t)}</span>
            {mode === "ops" && it.pending_ai_count > 0 && <AgentMark label={t("messages.row.ai_pending")} title={t("messages.row.ai_pending_title")} />}
          </button>
        );
      })}
    </div>
  ) : null;

  return (
    <div className="flex flex-col gap-2" data-panel="messages">
      {q.error && <ErrorState compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
      {picker}
      <div className="overflow-hidden surface-flat rounded-xl">
        <ThreadView key={current} threadId={current} mode={mode} embedded onChanged={changed} />
      </div>
    </div>
  );
}
