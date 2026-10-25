// One e-mail conversation for ops: who and what it is about, the decision waiting (if any), the mails, a reply box.
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle, ArrowLeft, ArrowRight, Check, ChevronRight, FileText, Loader2, Send, ShieldAlert,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState } from "@/components/app/states";
import { apiPost, fileUrl, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateShort, money } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { composerDraft, fileSize, messageTime, pct } from "@/features/messages/util";
import {
  INTENTS, IN_STATUS, OUT_STATUS, THREAD_STATE, decisionText, intentLabel, matchLabel, needsDecision, reasonText,
  stateLabel, tagLabel,
} from "./strings";

/** FI / EN language marker. */
export function LangBadge({ lang, className }) {
  const { t } = useT();
  if (lang !== "fi" && lang !== "en") return null;
  return (
    <span title={t("email.lang.title", { lang: t(`email.lang.name.${lang}`) })}
      className={cn("inline-flex h-5 items-center rounded-full border border-[color:var(--border-subtle)] px-1.5 text-[11px] font-medium text-ink-secondary", className)}>
      {t(`email.lang.${lang}`)}
    </span>
  );
}

/** Thread state as a workflow badge. */
export function ThreadStateBadge({ state, size = "sm" }) {
  const { t } = useT();
  return <StatusBadge size={size} state={THREAD_STATE[state] || "in-progress"} label={stateLabel(t, state)} />;
}

export function OwnerBadge({ size = "sm" }) {
  const { t } = useT();
  return <StatusBadge size={size} state="pending" label={t("email.row.owner")} />;
}

/** Linked records: quote / shipment / invoice, each a small link chip with its own status. */
function RefChips({ data, className }) {
  const { t, locale } = useT();
  const quoteId = data.quote?.id || data.quote_id;
  const quoteNo = data.quote?.number || data.quote_number;
  const shipmentId = data.shipment?.id || data.shipment_id;
  const shipmentRef = data.shipment?.reference || data.shipment_reference;
  const inv = data.invoice;
  if (!quoteId && !shipmentId && !inv) return null;
  const chip = "group inline-flex min-w-0 max-w-full items-center gap-2 surface-flat rounded-lg px-2.5 py-1 text-xs hover:bg-hover";
  return (
    <div className={cn("flex flex-wrap gap-1.5", className)}>
      {quoteId && (
        <Link to={`/ops/quotes/${quoteId}`} className={chip}>
          <span className="text-ink-tertiary">{t("email.thread.quote")}</span>
          <span className="font-mono text-[12px] font-medium">{quoteNo || "—"}</span>
          {data.quote?.status && <StatusBadge size="sm" group="quote" value={data.quote.status} />}
          {data.quote?.valid_until && <span className="hidden tabular text-ink-tertiary sm:inline">{t("email.thread.valid_until", { date: dateShort(data.quote.valid_until, locale) })}</span>}
          <ChevronRight className="size-3.5 text-ink-tertiary group-hover:text-foreground" aria-hidden />
        </Link>
      )}
      {shipmentId && (
        <Link to={`/ops/shipments/${shipmentId}`} className={chip}>
          <span className="text-ink-tertiary">{t("email.thread.shipment")}</span>
          <span className="font-mono text-[12px] font-medium">{shipmentRef || "—"}</span>
          {data.shipment?.status && <StatusBadge size="sm" group="status" value={data.shipment.status} />}
          {data.shipment?.eta && <span className="hidden tabular text-ink-tertiary sm:inline">{t("email.thread.eta", { date: dateShort(data.shipment.eta, locale) })}</span>}
          <ChevronRight className="size-3.5 text-ink-tertiary group-hover:text-foreground" aria-hidden />
        </Link>
      )}
      {inv && (
        <Link to={`/ops/billing/${inv.id}`} className={chip}>
          <span className="text-ink-tertiary">{t("email.thread.invoice")}</span>
          <span className="font-mono text-[12px] font-medium">{inv.number || "—"}</span>
          {inv.status && <StatusBadge size="sm" group="invoice" value={inv.status} />}
          {inv.total_cents !== null && inv.total_cents !== undefined && <span className="tabular text-ink-secondary">{money(inv.total_cents, locale)}</span>}
          <ChevronRight className="size-3.5 text-ink-tertiary group-hover:text-foreground" aria-hidden />
        </Link>
      )}
    </div>
  );
}

/** Customer line: name, address, company + KYC / sanctions. */
function CustomerLine({ data }) {
  const { t } = useT();
  const company = data.company;
  const name = company?.name || data.customer_name;
  const sanctionsOpen = company?.sanctions_status && !["clear", "not_screened"].includes(company.sanctions_status);
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-tertiary">
      {data.company_id ? (
        <Link to={`/ops/customers/${data.company_id}`} className="font-medium text-ink-secondary hover:text-foreground hover:underline">{name || "—"}</Link>
      ) : (
        <span className="font-medium text-ink-secondary">{name || t("email.thread.no_company")}</span>
      )}
      {data.customer_email && <span className="min-w-0 truncate">{data.customer_email}</span>}
      {company?.kyc_status && (
        <span className="inline-flex items-center gap-1">
          <span>{t("email.thread.kyc")}</span>
          <StatusBadge size="sm" group="kyc" value={company.kyc_status} icon={false} />
        </span>
      )}
      {sanctionsOpen && <span className="text-danger-fg">{t("email.thread.sanctions", { status: tEnum(t, "sanctions", company.sanctions_status) })}</span>}
      {company?.test_tenant && <StatusBadge size="sm" state="expired" icon={false} label={t("email.thread.test_tenant")} />}
    </div>
  );
}

/** Owner decision for one inbound mail: why it stopped, and "handle as <intent>" / read again / dismiss. */
function OwnerDecision({ m, canAct, onDone, onShow }) {
  const { t, locale } = useT();
  const [intent, setIntent] = useState(INTENTS.includes(m.intent) && m.intent !== "other" ? m.intent : "");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const injection = m.decision === "possible_prompt_injection" || m.parse?.injection_suspected || (m.injection_markers || []).length > 0;
  const reason = m.status === "failed" ? reasonText(t, "processing_failed") : reasonText(t, m.decision || m.owner_card?.reason);
  const run = async (kind) => {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "dismiss") await apiPost(`/email-channel/messages/${m.id}/dismiss`, {});
      else await apiPost(`/email-channel/messages/${m.id}/reprocess`, kind === "apply" ? { intent } : {});
      await onDone?.(kind);
    } catch (e) {
      setError(t("email.owner.failed", { error: e?.message || t("state.error") }));
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="rounded-xl px-3 py-2.5 status-pending" role="region" aria-label={t("email.owner.title")} data-owner-decision={m.id}>
      <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
        <div className="flex min-w-0 flex-1 basis-[260px] items-start gap-2">
          {injection ? <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden /> : <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />}
          <div className="min-w-0">
            <p className="text-sm font-medium">{reason}</p>
            <p className="flex flex-wrap items-center gap-x-1.5 text-xs opacity-90">
              <span className="min-w-0 truncate">{m.from_name || m.from_email}</span>
              <span aria-hidden>·</span>
              <time className="tabular" dateTime={m.created_at}>{messageTime(m.created_at, locale)}</time>
              {m.intent && <><span aria-hidden>·</span><span>{intentLabel(t, m.intent)}</span></>}
              <button type="button" onClick={() => onShow?.(m.id)} className="rounded px-0.5 underline underline-offset-2">{t("email.owner.show")}</button>
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={intent || undefined} onValueChange={setIntent} disabled={!canAct || !!busy}>
            <SelectTrigger className="h-7 w-[200px] bg-background" aria-label={t("email.owner.treat_as")}>
              <SelectValue placeholder={t("email.owner.pick")} />
            </SelectTrigger>
            <SelectContent>
              {INTENTS.map((i) => <SelectItem key={i} value={i}>{intentLabel(t, i)}</SelectItem>)}
            </SelectContent>
          </Select>
          <Button size="sm" disabled={!canAct || !intent || !!busy} onClick={() => run("apply")}>
            {busy === "apply" && <Loader2 className="animate-spin" />}{t("email.owner.apply")}
          </Button>
          <Button size="sm" variant="outline" className="bg-background" disabled={!canAct || !!busy} onClick={() => run("reread")}>
            {busy === "reread" && <Loader2 className="animate-spin" />}{t("email.owner.reread")}
          </Button>
          <Button size="sm" variant="ghost" disabled={!canAct || !!busy} onClick={() => run("dismiss")}>
            {busy === "dismiss" && <Loader2 className="animate-spin" />}{t("email.owner.dismiss")}
          </Button>
        </div>
      </div>
      {busy && <p className="mt-1.5 text-xs" aria-live="polite">{t("email.owner.busy")}</p>}
      {error && <ErrorState compact className="mt-2" error={{ message: error }} />}
    </div>
  );
}

function Attachments({ m }) {
  const { locale } = useT();
  const items = m.attachments || [];
  if (!items.length) return null;
  return (
    <ul className="mt-2 flex flex-wrap gap-1.5">
      {items.map((a, i) => {
        const body = (
          <>
            <FileText className="size-3.5 shrink-0 text-ink-tertiary" aria-hidden />
            <span className="truncate">{a.filename || `#${i + 1}`}</span>
            {a.size ? <span className="shrink-0 tabular text-ink-tertiary">{fileSize(a.size, locale)}</span> : null}
          </>
        );
        const cls = "inline-flex h-7 max-w-full items-center gap-1.5 surface-flat rounded-lg px-2 text-xs";
        return (
          <li key={`${a.file_id || a.filename}-${i}`} className="max-w-full">
            {a.file_id ? (
              <a href={fileUrl(`/email-channel/messages/${m.id}/attachments/${i}`)} className={cn(cls, "hover:bg-hover")} download={a.filename || true}>{body}</a>
            ) : <span className={cls}>{body}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function HeaderRow({ label, value, mono }) {
  return (
    <div className="grid grid-cols-[minmax(96px,auto)_1fr] gap-x-3">
      <dt className="text-ink-tertiary">{label}</dt>
      <dd className={cn("min-w-0 break-all", mono && "font-mono text-[11px]")}>{value || "—"}</dd>
    </div>
  );
}

/** One e-mail. Inbound sits left on a quiet surface; outbound right on the subtle fill. */
export function EmailMessage({ m, highlight, compact }) {
  const { t, locale } = useT();
  const inbound = m.direction === "in";
  const hasQuoted = inbound && m.new_text && m.text && m.text.replace(/\s+/g, "").length > m.new_text.replace(/\s+/g, "").length + 8;
  const [full, setFull] = useState(false);
  const body = inbound ? (full ? m.text : m.new_text || m.text) : m.text;
  const injection = (m.injection_markers || []).length > 0 || m.parse?.injection_suspected;
  const statusVisual = inbound ? IN_STATUS[m.status] : OUT_STATUS[m.delivery?.status || m.status];
  const statusKey = inbound ? `email.in.${m.status}` : `email.out.${m.delivery?.status || m.status}`;
  const statusText = t(statusKey) === statusKey ? null : t(statusKey);
  // A mail waiting for a decision already shows its reason in the decision bar above.
  const decision = inbound && m.status !== "needs_owner" ? decisionText(t, m.decision) : null;
  const tag = !inbound ? tagLabel(t, m.tag) : null;
  const deliveryError = !inbound && (m.delivery?.error || m.error);
  const conf = m.parse?.confidence;
  const author = !inbound ? (m.kind === "ops" && m.author?.name ? m.author.name : t("email.author.auto")) : null;
  const to = (m.to || []).join(", ");
  return (
    <article id={`email-msg-${m.id}`} data-direction={m.direction} data-message-id={m.id}
      className={cn("flex max-w-[min(720px,94%)] scroll-mt-4 flex-col", inbound ? "self-start items-start" : "self-end items-end")}>
      <header className={cn("mb-1 flex max-w-full flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-ink-tertiary", !inbound && "justify-end")}>
        <span className="inline-flex h-5 items-center rounded-full bg-secondary px-1.5 text-[11px] font-medium text-ink-secondary">
          {inbound ? t("email.msg.in") : t("email.msg.out")}
        </span>
        <span className="min-w-0 truncate font-medium text-ink-secondary">{inbound ? (m.from_name || m.from_email) : author}</span>
        {inbound && m.from_name && <span className="hidden min-w-0 truncate sm:inline">{m.from_email}</span>}
        {!inbound && to && <span className="min-w-0 truncate">→ {to}</span>}
        <time dateTime={m.created_at} className="tabular">{messageTime(m.created_at, locale)}</time>
      </header>
      <div className={cn("max-w-full rounded-xl px-3 py-2 text-sm", inbound ? "surface-flat" : "bg-[var(--bg-subtle)]",
        highlight && "shadow-[inset_0_0_0_1px_var(--border-strong)]")}>
        <p className="mb-1 break-words font-medium">{m.subject || t("email.msg.no_subject")}</p>
        {injection && (
          <div className="mb-2 rounded-lg px-2.5 py-1.5 text-xs status-flagged" role="note">
            <p className="flex items-center gap-1.5 font-medium"><ShieldAlert className="size-3.5" aria-hidden />{t("email.injection.title")}</p>
            <p className="mt-0.5">{t("email.injection.body")}</p>
            {(m.injection_markers || []).length > 0 && (
              <ul className="mt-1 flex flex-col gap-0.5">
                {m.injection_markers.slice(0, 6).map((x, i) => <li key={i} className="break-words font-mono text-[11px]">“{String(x).slice(0, 160)}”</li>)}
              </ul>
            )}
          </div>
        )}
        {body ? <p className={cn("whitespace-pre-wrap break-words", compact && !full && "line-clamp-[12]")}>{body.replace(/\r\n/g, "\n").trim()}</p>
          : <p className="text-ink-tertiary">{t("email.row.no_preview")}</p>}
        {hasQuoted && (
          <button type="button" onClick={() => setFull((f) => !f)} aria-expanded={full}
            className="mt-1 rounded px-0.5 text-xs text-ink-secondary underline-offset-2 hover:underline">
            {full ? t("email.msg.show_new") : t("email.msg.show_full")}
          </button>
        )}
        <Attachments m={m} />
      </div>
      <div className={cn("mt-1 flex max-w-full flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-tertiary", !inbound && "justify-end")}>
        {inbound && m.intent && (
          <span className="inline-flex h-5 items-center gap-1 rounded-full bg-secondary px-1.5 text-[11px] font-medium text-ink-secondary">
            {intentLabel(t, m.intent)}
            {conf !== null && conf !== undefined && <span className="tabular font-normal text-ink-tertiary">{pct(conf, locale)}</span>}
          </span>
        )}
        {inbound && m.parse?.model && m.parse.model !== "rules" && <AgentMark />}
        {inbound && m.parse?.language && <LangBadge lang={m.parse.language} />}
        {tag && <span>{tag}</span>}
        {decision && <span className="text-ink-secondary">{decision}</span>}
        {statusVisual && statusText && <StatusBadge size="sm" state={statusVisual} label={statusText} />}
        {!statusVisual && !inbound && statusText && <span className="inline-flex items-center gap-1"><Check className="size-3" aria-hidden />{statusText}</span>}
        <details className="group">
          <summary className="cursor-pointer list-none rounded px-0.5 text-ink-secondary underline-offset-2 hover:underline [&::-webkit-details-marker]:hidden">
            {t("email.msg.headers")}
          </summary>
          <dl className="mt-1 flex w-[min(560px,80vw)] flex-col gap-0.5 rounded-lg border border-[color:var(--border-subtle)] px-2.5 py-2 text-left text-xs text-ink-secondary">
            <HeaderRow label={t("email.msg.h.message_id")} value={m.message_id} mono />
            <HeaderRow label={t("email.msg.h.in_reply_to")} value={m.in_reply_to} mono />
            {(m.references || []).length > 0 && <HeaderRow label={t("email.msg.h.references")} value={m.references.join(" ")} mono />}
            {inbound && <HeaderRow label={t("email.msg.h.match")} value={matchLabel(t, m.match)} />}
            {inbound && m.source && <HeaderRow label={t("email.msg.h.source")} value={t(`email.source.${m.source}`) === `email.source.${m.source}` ? m.source : t(`email.source.${m.source}`)} />}
            {(m.cc || []).length > 0 && <HeaderRow label={t("email.msg.h.cc")} value={m.cc.join(", ")} />}
            {inbound && m.parse?.model && (
              <HeaderRow label={t("email.msg.h.model")} value={m.parse.model === "rules" ? t("email.msg.h.model_rules") : t("email.msg.h.model_llm")} />
            )}
            {inbound && m.parse?.summary && <HeaderRow label={t("email.msg.h.summary")} value={m.parse.summary} />}
          </dl>
        </details>
      </div>
      {deliveryError && <p className="mt-1 text-xs text-danger-fg">{t("email.delivery.failed", { error: deliveryError })}</p>}
      {inbound && m.status === "retry" && <p className="mt-1 text-xs text-ink-tertiary">{t("email.owner.retry_note")}</p>}
    </article>
  );
}

/** Manual ops reply. Text survives thread switches and polls; a failed send keeps it and offers Retry. */
function ReplyBox({ data, canReply, onSent, rows = 3 }) {
  const { t } = useT();
  const key = `email:${data.id}`;
  const [text, setText] = useState(() => composerDraft.get(key));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [sent, setSent] = useState(false);
  useEffect(() => { composerDraft.set(key, text); }, [key, text]);
  useEffect(() => {
    if (!sent) return undefined;
    const id = setTimeout(() => setSent(false), 4000);
    return () => clearTimeout(id);
  }, [sent]);
  const send = async () => {
    if (busy || !canReply || !text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await apiPost(`/email-channel/threads/${data.id}/reply`, { text: text.trim() });
      setText("");
      composerDraft.set(key, "");
      setSent(true);
      await onSent?.();
    } catch (e) {
      setError(e?.status === 0 ? t("state.offline") : e?.message || t("state.error"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-2">
      <Textarea value={text} onChange={(e) => setText(e.target.value)} rows={rows} disabled={!canReply || busy}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } }}
        aria-label={t("email.reply.label")} placeholder={t("email.reply.placeholder", { email: data.customer_email || "—" })}
        className="min-h-[60px] resize-y" />
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 text-xs text-ink-tertiary" aria-live="polite">
          {busy ? t("email.reply.sending")
            : sent ? <span className="inline-flex items-center gap-1 text-success-fg"><Check className="size-3.5" aria-hidden />{t("email.reply.sent")}</span>
            : !canReply ? t("email.reply.no_permission")
            : <span className="hidden sm:inline">{t("email.reply.hint")}</span>}
        </span>
        <Button size="sm" onClick={send} disabled={!canReply || busy || !text.trim()}>
          {busy ? <Loader2 className="animate-spin" /> : <Send />} {t("email.reply.send")}
        </Button>
      </div>
      {error && <ErrorState compact error={{ message: t("email.reply.failed", { error }) }} onRetry={send} />}
    </div>
  );
}

/**
 * The conversation body, given loaded thread data (from /email-channel/threads/{id} or /email-channel/for).
 * compact: embedded on a shipment / quote page (smaller header, bounded height, link to the inbox).
 */
export function EmailConversation({ data, reload, refreshFailed, compact = false, backTo, focusMessageId, onChanged, className }) {
  const { t } = useT();
  const { can } = useAuth();
  const canAct = can("messages.ops");
  const scrollRef = useRef(null);
  const positioned = useRef(null);
  const messages = data.messages || [];
  const decisions = messages.filter(needsDecision);
  const [focus, setFocus] = useState(focusMessageId || null);

  const show = (mid) => {
    setFocus(mid);
    const el = document.getElementById(`email-msg-${mid}`);
    el?.scrollIntoView({ block: "start", behavior: "smooth" });
  };

  // First render of a thread: go to the focused mail, else to the latest one. Later polls keep the reader's position.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || positioned.current === data.id || !messages.length) return;
    positioned.current = data.id;
    const target = focusMessageId && document.getElementById(`email-msg-${focusMessageId}`);
    if (target) target.scrollIntoView({ block: "start" });
    else el.scrollTop = el.scrollHeight;
  }, [data.id, messages.length, focusMessageId]);

  const changed = async (kind) => {
    await reload?.();
    onChanged?.(kind, data.id);
  };

  const title = data.subject || data.ref || data.customer_email || "—";

  return (
    <div className={cn("flex min-h-0 flex-col", !compact && "h-full", className)} data-email-thread={data.id}>
      <div className={cn("shrink-0 border-b border-[color:var(--border-subtle)]", compact ? "px-3 py-2" : "px-3 py-2.5 md:px-4")}>
        <div className="flex items-start gap-2">
          {backTo && (
            <Button asChild variant="ghost" size="icon-sm" className="-ml-1 mt-0.5 md:hidden">
              <Link to={backTo} aria-label={t("email.thread.back")}><ArrowLeft /></Link>
            </Button>
          )}
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              {compact ? <h3 className="min-w-0 truncate text-sm font-medium">{title}</h3> : <h2 className="min-w-0 truncate text-md font-medium">{title}</h2>}
              <ThreadStateBadge state={data.state} />
              {data.needs_owner && <OwnerBadge />}
              <LangBadge lang={data.language} />
            </div>
            <CustomerLine data={data} />
          </div>
          {compact && (
            <Button asChild size="sm" variant="ghost" className="shrink-0">
              <Link to={`/ops/email/${data.id}`}><span className="hidden sm:inline">{t("email.thread.open_in_inbox")}</span> <ArrowRight /></Link>
            </Button>
          )}
        </div>
        <RefChips data={data} className="mt-2" />
      </div>

      {refreshFailed && <ErrorState compact className="mx-3 mt-2" error={{ message: t("state.failed_refresh") }} onRetry={reload} />}

      {decisions.length > 0 && (
        <div className={cn("flex shrink-0 flex-col gap-2 border-b border-[color:var(--border-subtle)]", compact ? "p-2" : "px-3 py-2.5 md:px-4")}>
          {decisions.map((m) => <OwnerDecision key={m.id} m={m} canAct={canAct} onDone={changed} onShow={show} />)}
        </div>
      )}

      <div ref={scrollRef} className={cn("min-h-0 overflow-y-auto px-3 py-4 md:px-4", compact ? "max-h-[560px]" : "flex-1")}>
        <div className="flex flex-col gap-5">
          {messages.length === 0 && <p className="text-sm text-ink-tertiary">{t("email.thread.empty")}</p>}
          {messages.map((m) => <EmailMessage key={m.id} m={m} compact={compact} highlight={focus === m.id} />)}
        </div>
      </div>

      <div className={cn("shrink-0 border-t border-[color:var(--border-subtle)]", compact ? "px-3 py-2" : "px-3 py-3 md:px-4")}>
        <ReplyBox key={data.id} data={data} canReply={canAct} onSent={() => changed("replied")} rows={compact ? 2 : 3} />
      </div>
    </div>
  );
}

function ThreadSkeleton() {
  return (
    <div className="flex flex-1 flex-col gap-4 p-4" aria-busy="true">
      <Skeleton className="h-5 w-1/2 rounded-md" />
      <Skeleton className="h-4 w-1/3 rounded-md" />
      <Skeleton className="h-24 w-2/3 rounded-xl" />
      <Skeleton className="ml-auto h-20 w-2/3 rounded-xl" />
      <Skeleton className="h-16 w-1/2 rounded-xl" />
    </div>
  );
}

/** Loads one thread and renders it (inbox reading pane). Polls every 15 s. */
export default function EmailThreadView({ threadId, backTo, focusMessageId, onChanged }) {
  const { t } = useT();
  const q = useApi(threadId ? `/email-channel/threads/${threadId}` : null, { pollMs: 15000 });
  const data = q.data && q.data.id === threadId ? q.data : null;
  if (!threadId) return null;
  if (!data && q.error) {
    return (
      <div className="flex flex-1 flex-col gap-3 p-4">
        {backTo && (
          <Button asChild variant="ghost" size="sm" className="self-start md:hidden">
            <Link to={backTo}><ArrowLeft /> {t("email.thread.back")}</Link>
          </Button>
        )}
        {q.error.status === 404 ? <EmptyState title={t("email.thread.not_found")} /> : <ErrorState error={q.error} onRetry={q.reload} />}
      </div>
    );
  }
  if (!data) return <ThreadSkeleton />;
  return (
    <EmailConversation data={data} reload={q.reload} refreshFailed={!!q.error} backTo={backTo} focusMessageId={focusMessageId}
      onChanged={onChanged} />
  );
}
