import React, { useState } from "react";
import { FileText, Lock, Package, XCircle } from "lucide-react";
import { AgentMark } from "@/components/app/StatusBadge";
import { fileUrl } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { fileSize, isOwnSide, messageTime, reasonText } from "./util";

/** Sender role tag: Asiakas / Huolitsija / AI / Järjestelmä. AI is the violet provenance chip. */
export function RoleTag({ type, mode }) {
  const { t } = useT();
  if (type === "ai") {
    return <AgentMark label={t("messages.role.ai")} title={mode === "ops" ? t("messages.role.ai_title_ops") : t("messages.role.ai_title_customer")} />;
  }
  const key = ["customer", "ops", "system"].includes(type) ? type : "system";
  return (
    <span className="inline-flex h-5 items-center rounded-full bg-secondary px-1.5 text-[11px] font-medium text-ink-secondary">
      {t(`messages.role.${key}`)}
    </span>
  );
}

export function AttachmentChips({ items, className }) {
  const { locale } = useT();
  if (!items?.length) return null;
  return (
    <ul className={cn("flex flex-wrap gap-1.5", className)}>
      {items.map((a) => (
        <li key={a.document_id} className="max-w-full">
          <a href={fileUrl(`/documents/${a.document_id}/file`)} target="_blank" rel="noreferrer"
            className="inline-flex h-7 max-w-full items-center gap-1.5 surface-flat rounded-lg px-2 text-xs hover:bg-hover">
            <FileText className="size-3.5 shrink-0 text-ink-tertiary" aria-hidden />
            <span className="truncate">{a.filename || a.document_id}</span>
            {a.size ? <span className="shrink-0 tabular text-ink-tertiary">{fileSize(a.size, locale)}</span> : null}
          </a>
        </li>
      ))}
    </ul>
  );
}

/** Automatic status update: lighter inline event row, not a chat bubble. */
function UpdateRow({ m, mode }) {
  const { t, locale } = useT();
  return (
    <div className="flex items-start gap-2 self-stretch rounded-lg px-2 py-1 text-xs text-ink-secondary" data-kind="update">
      <Package className="mt-0.5 size-3.5 shrink-0 text-ink-tertiary" aria-label={t("messages.thread.update")} />
      <p className="min-w-0 flex-1 whitespace-pre-wrap break-words">{m.body}</p>
      <span className="flex shrink-0 items-center gap-1.5 text-ink-tertiary">
        <span className="hidden sm:inline">{t(`messages.role.${m.author?.type === "ai" ? "ai" : "system"}`)}</span>
        <time dateTime={m.sent_at || m.created_at} className="tabular">{messageTime(m.sent_at || m.created_at, locale)}</time>
      </span>
      {mode === "ops" && m.visibility === "internal" && <Lock className="mt-0.5 size-3 shrink-0" aria-label={t("messages.thread.internal")} />}
    </div>
  );
}

/** Rejected AI draft (ops only): collapsed row, text on demand. */
function RejectedRow({ m }) {
  const { t, locale } = useT();
  const [open, setOpen] = useState(false);
  const who = m.rejected_by?.name;
  return (
    <div className="self-stretch rounded-lg px-2 py-1 text-xs text-ink-tertiary" data-kind="rejected">
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <XCircle className="size-3.5 shrink-0" aria-hidden />
        <span className="font-medium text-ink-secondary">{t("messages.thread.rejected_draft")}</span>
        {m.author?.type === "ai" && <AgentMark />}
        {m.rejected_reason && <span className="min-w-0 truncate">· {reasonText(m.rejected_reason, t)}</span>}
        {who && <span>· {who}</span>}
        <time className="tabular">· {messageTime(m.rejected_at || m.created_at, locale)}</time>
        <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
          className="ml-1 rounded px-1 text-ink-secondary underline-offset-2 hover:underline">
          {open ? t("messages.thread.hide_draft") : t("messages.thread.show_draft")}
        </button>
      </div>
      {open && <p className="mt-1 whitespace-pre-wrap break-words pl-5 text-ink-secondary">{m.body}</p>}
    </div>
  );
}

/** One message in the timeline. Pending drafts are rendered by AiDraftCard, not here. */
export default function MessageItem({ m, mode }) {
  const { t, locale } = useT();
  const [showOriginal, setShowOriginal] = useState(false);
  if (m.status === "pending_approval") return null;
  if (m.status === "rejected") return mode === "ops" ? <RejectedRow m={m} /> : null;
  if (m.kind === "update") return <UpdateRow m={m} mode={mode} />;
  const type = m.author?.type || "system";
  const own = isOwnSide(mode, type);
  const internal = m.visibility === "internal";
  const roleLabel = t(`messages.role.${["customer", "ops", "ai", "system"].includes(type) ? type : "system"}`);
  const name = !m.author?.name || m.author.name === roleLabel ? null : m.author.name;
  const at = m.sent_at || m.created_at;
  // `edited` is only set when the text changed at approval; an edit saved earlier (PATCH) leaves original_body.
  const edited = !!(m.edited || (m.original_body && m.original_body.trim() !== (m.body || "").trim()));
  const opsMeta = mode === "ops" && (edited || m.approved_by?.name);
  return (
    <article className={cn("flex max-w-[min(680px,92%)] flex-col", own ? "self-end items-end" : "self-start items-start")}
      data-author={type} data-visibility={m.visibility}>
      <header className={cn("mb-1 flex flex-wrap items-center gap-1.5 text-xs text-ink-tertiary", own && "justify-end")}>
        <RoleTag type={type} mode={mode} />
        {name && <span className="font-medium text-ink-secondary">{name}</span>}
        <time dateTime={at} className="tabular">{messageTime(at, locale)}</time>
      </header>
      <div className={cn("max-w-full rounded-xl px-3 py-2 text-sm",
        internal ? "border border-dashed border-[color:var(--border-strong)] bg-[var(--bg-subtle)]"
          : own ? "bg-[var(--bg-subtle)]" : "surface-flat")}>
        {internal && (
          <p className="mb-1 flex flex-wrap items-center gap-1 text-xs">
            <Lock className="size-3.5" aria-hidden />
            <span className="font-medium">{t("messages.thread.internal")}</span>
            <span className="text-ink-tertiary">· {t("messages.thread.internal_hint")}</span>
          </p>
        )}
        {m.body && <p className="whitespace-pre-wrap break-words">{m.body}</p>}
        <AttachmentChips items={m.attachments} className={m.body ? "mt-2" : ""} />
      </div>
      {opsMeta && (
        <div className={cn("mt-1 flex flex-wrap items-center gap-x-1.5 text-xs text-ink-tertiary", own && "justify-end")}>
          {m.approved_by?.name && <span>{t("messages.thread.approved_by", { name: m.approved_by.name })}</span>}
          {edited && (
            <>
              {m.approved_by?.name && <span aria-hidden>·</span>}
              <span>{t("messages.thread.edited")}</span>
              {m.original_body && (
                <button type="button" onClick={() => setShowOriginal((o) => !o)} aria-expanded={showOriginal}
                  className="rounded px-0.5 text-ink-secondary underline-offset-2 hover:underline">
                  {showOriginal ? t("messages.thread.hide_original") : t("messages.thread.show_original")}
                </button>
              )}
            </>
          )}
        </div>
      )}
      {showOriginal && m.original_body && (
        <p className="mt-1 max-w-full whitespace-pre-wrap break-words rounded-lg border border-dashed px-3 py-2 text-xs text-ink-secondary">{m.original_body}</p>
      )}
    </article>
  );
}
