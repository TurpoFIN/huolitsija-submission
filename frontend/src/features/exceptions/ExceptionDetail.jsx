// Ops detail pane for one exception: facts, AI assessment, customer message draft (approve/edit/reject),
// owner/severity/status, resolve, history.
import { useConfirmChange } from "@/components/app/ConfirmChange";
import { aiErrorInfo } from "@/lib/aiDisplay";
import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Loader2, Sparkles, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { KV } from "@/components/app/layout";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { Timeline } from "@/components/app/Timeline";
import { apiPatch, apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { DetectedBy, SavedNote, SlaDue, useFlag, useNow } from "./common";
import ResolveDialog from "./ResolveDialog";
import { SEVERITIES, evidenceItems, historyEvents, localDescription, localResolution, localTitle, resolverName, typeLabel } from "./text";

function Block({ title, right, children, className }) {
  return (
    <section className={cn("border-t border-[color:var(--border-subtle)] px-4 py-3", className)}>
      {(title || right) && (
        <div className="mb-2 flex items-center justify-between gap-2">
          {title && <h3 className="flex items-center gap-1.5 text-sm font-medium">{title}</h3>}
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

function AiAssessment({ ex, canWrite, onAssessed, onSeverity }) {
  const { t, locale } = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const a = ex.ai_assessment;
  const assess = async () => {
    setBusy(true);
    setError(null);
    try {
      await apiPost(`/exceptions/${ex.id}/assess`, {});
      onAssessed();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const assessButton = (label) => canWrite && (
    <Button size="xs" variant={a?.status === "ok" ? "ghost" : "outline"} onClick={assess} disabled={busy}>
      <Sparkles /> {label}
    </Button>
  );
  return (
    <Block
      title={<><AgentMark /> {t("exceptions.ai.title")}</>}
      right={a?.status === "ok" && !busy ? assessButton(t("exceptions.ai.reassess")) : null}
    >
      {busy ? (
        <p className="flex items-center gap-2 text-sm text-ink-secondary" aria-live="polite">
          <Loader2 className="size-4 animate-spin" /> {t("exceptions.ai.busy")}
        </p>
      ) : !a ? (
        <div className="flex flex-col items-start gap-2">
          <p className="text-sm text-ink-tertiary">{t("exceptions.ai.none")}</p>
          {assessButton(t("exceptions.ai.assess"))}
        </div>
      ) : a.status !== "ok" ? (
        <div className="flex flex-col items-start gap-2">
          <p className="text-sm text-ink-secondary">{a.status === "unavailable" ? t("exceptions.ai.unavailable") : t("exceptions.ai.error")}</p>
          {a.note && <p className="text-xs text-ink-tertiary">{aiErrorInfo(a,t).reason} {aiErrorInfo(a,t).next}</p>}
          {assessButton(t("exceptions.ai.assess"))}
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {a.summary && <p className="text-sm">{a.summary}</p>}
          {a.impact && (
            <div>
              <p className="text-xs text-ink-tertiary">{t("exceptions.ai.impact")}</p>
              <p className="text-sm">{a.impact}</p>
            </div>
          )}
          {a.recommended_actions?.length > 0 && (
            <div>
              <p className="text-xs text-ink-tertiary">{t("exceptions.ai.actions")}</p>
              <ol className="mt-1 flex list-decimal flex-col gap-1 pl-5 text-sm marker:text-ink-tertiary">
                {a.recommended_actions.map((s, i) => <li key={i}>{s}</li>)}
              </ol>
            </div>
          )}
          {a.severity_suggestion && (
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-xs text-ink-tertiary">{t("exceptions.ai.severity_suggestion")}</span>
              <StatusBadge size="sm" group="severity" value={a.severity_suggestion} />
              {a.severity_suggestion === ex.severity ? (
                <span className="text-xs text-ink-tertiary">{t("exceptions.ai.same")}</span>
              ) : canWrite && ex.status !== "resolved" ? (
                <Button size="xs" variant="outline" onClick={() => onSeverity(a.severity_suggestion)}>{t("exceptions.ai.apply")}</Button>
              ) : null}
            </div>
          )}
          <p className="text-xs text-ink-tertiary">{t("exceptions.ai.assessed", { time: dateTime(a.assessed_at, locale) })}</p>
        </div>
      )}
      {error && <ErrorState className="mt-2" compact error={error} onRetry={assess} />}
    </Block>
  );
}

const REJECT_REASONS = [
  [/^Replaced by a newer( AI)? draft/i, "exceptions.msg.reason.replaced"],
  [/^Exception cleared before approval/i, "exceptions.msg.reason.cleared"],
  [/^Resolved with a person's message/i, "exceptions.msg.reason.resolved"],
  [/^Rejected by a person$/i, "exceptions.msg.reason.person"],
];

function rejectReason(reason, t) {
  const hit = REJECT_REASONS.find(([re]) => re.test(reason));
  return t("exceptions.msg.reason", { reason: hit ? t(hit[1]) : reason });
}

function CustomerMessage({ ex, canApprove, onChanged }) {
  const { t, locale } = useT();
  const m = ex.ai_message;
  const [body, setBody] = useState(m?.body || "");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [saved, flagSaved] = useFlag();
  useEffect(() => { setBody(m?.body || ""); setRejecting(false); setNote(""); }, [m?.id, m?.body]);

  if (!m) {
    if (ex.customer_visible === false) return <Block title={t("exceptions.msg.title")}><p className="text-sm text-ink-tertiary">{t("exceptions.msg.internal")}</p></Block>;
    if (ex.ai_assessment?.status === "ok" && !ex.ai_assessment.customer_message_draft)
      return <Block title={t("exceptions.msg.title")}><p className="text-sm text-ink-tertiary">{t("exceptions.ai.no_draft")}</p></Block>;
    return null;
  }
  const dirty = body.trim() !== (m.body || "").trim();
  const run = async (kind, fn) => {
    setBusy(kind);
    setError(null);
    try {
      await fn();
      return true;
    } catch (e) {
      setError(e);
      return false;
    } finally {
      setBusy(null);
    }
  };
  const save = () => run("save", () => apiPatch(`/messages/${m.id}`, { body: body.trim() })).then((ok) => { if (ok) { flagSaved(); onChanged(); } });
  const approve = () => {
    if (!body.trim()) { setError({ message: t("exceptions.msg.empty") }); return; }
    run("approve", () => apiPost(`/messages/${m.id}/approve`, { body: body.trim() })).then((ok) => {
      if (ok) { toast.success(t("exceptions.msg.sent", { time: "" }).trim()); onChanged(); }
    });
  };
  const reject = () => run("reject", () => apiPost(`/messages/${m.id}/reject`, { note: note.trim() || null })).then((ok) => { if (ok) onChanged(); });
  const thread = m.thread_id ? (
    <Button variant="link" size="sm" className="h-auto px-0" asChild><Link to={`/ops/messages/${m.thread_id}`}>{t("exceptions.msg.open_thread")}</Link></Button>
  ) : null;

  if (m.status !== "pending_approval") {
    return (
      <Block title={t("exceptions.msg.title")} right={thread}>
        {m.status === "sent" ? (
          <>
            <p className="mb-2 text-xs text-ink-tertiary">{t("exceptions.msg.sent", { time: dateTime(m.sent_at || m.created_at, locale) })}</p>
            <div className="whitespace-pre-wrap rounded-lg bg-subtle p-3 text-sm">{m.body}</div>
          </>
        ) : (
          <>
            <p className="text-sm text-ink-tertiary">{t("exceptions.msg.rejected")}</p>
            {m.rejected_reason && <p className="mt-1 text-xs text-ink-tertiary">{rejectReason(m.rejected_reason, t)}</p>}
          </>
        )}
      </Block>
    );
  }
  return (
    <Block title={<>{t("exceptions.msg.title")} <StatusBadge size="sm" group="approval" value="pending" /></>} right={thread}>
      <p className="mb-2 flex items-center gap-1.5 text-xs text-ink-secondary"><AgentMark /> {t("exceptions.msg.pending")}</p>
      <Textarea value={body} onChange={(e) => setBody(e.target.value)} className="min-h-[150px]" aria-label={t("exceptions.msg.title")}
        disabled={!canApprove || !!busy} />
      {canApprove ? (
        rejecting ? (
          <div className="mt-2 flex flex-col gap-2">
            <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("exceptions.msg.reject_note")} aria-label={t("exceptions.msg.reject_note")} autoFocus />
            <div className="flex gap-2">
              <Button size="sm" variant="destructive" onClick={reject} disabled={!!busy}>{busy === "reject" ? t("state.saving") : t("exceptions.msg.reject")}</Button>
              <Button size="sm" variant="ghost" onClick={() => setRejecting(false)} disabled={!!busy}>{t("action.cancel")}</Button>
            </div>
          </div>
        ) : (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={approve} disabled={!!busy}>{busy === "approve" ? t("state.saving") : t("exceptions.msg.approve")}</Button>
            <Button size="sm" variant="outline" onClick={save} disabled={!!busy || !dirty} title={!dirty ? t("exceptions.msg.save") : undefined}>
              {busy === "save" ? t("state.saving") : t("exceptions.msg.save")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(true)} disabled={!!busy}>{t("exceptions.msg.reject")}</Button>
            <SavedNote show={saved} />
            {m.edited_by && !saved && <span className="text-xs text-ink-tertiary">{t("exceptions.msg.edited")} · {m.edited_by.name}</span>}
          </div>
        )
      ) : (
        <p className="mt-2 text-xs text-ink-tertiary">{t("exceptions.msg.no_perm")}</p>
      )}
      {error && <ErrorState className="mt-2" compact error={error} />}
    </Block>
  );
}

function NoteForm({ id, onDone }) {
  const { t } = useT();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [saved, flagSaved] = useFlag();
  const add = async () => {
    if (!note.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await apiPatch(`/exceptions/${id}`, { note: note.trim() });
      setNote("");
      flagSaved();
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-3 flex flex-col gap-1">
      <div className="flex gap-2">
        <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("exceptions.d.note_placeholder")} aria-label={t("exceptions.d.note")}
          onKeyDown={(e) => { if (e.key === "Enter") add(); }} maxLength={1000} />
        <Button size="sm" variant="outline" className="h-8" onClick={add} disabled={busy || !note.trim()}>{busy ? t("state.saving") : t("exceptions.d.note_add")}</Button>
      </div>
      <SavedNote show={saved} />
      {error && <ErrorState compact error={error} />}
    </div>
  );
}

export default function ExceptionDetail({ id, onChanged, onClose }) {
  const { t, locale } = useT();
  const confirmation = useConfirmChange();
  const { user, can } = useAuth();
  const now = useNow();
  const q = useApi(id ? `/exceptions/${id}` : null);
  const assignees = useApi(can("exceptions.write") ? "/exceptions/assignees" : null);
  const [resolveOpen, setResolveOpen] = useState(false);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [saved, flagSaved] = useFlag();
  const canWrite = can("exceptions.write");
  const canApprove = can("messages.ops");

  const refresh = () => { q.reload(); onChanged?.(); };
  const patch = async (kind, body, confirmed = false) => {
    if (!confirmed) { confirmation.confirm(() => patch(kind, body, true)); return; }
    setBusy(kind);
    setError(null);
    try {
      await apiPatch(`/exceptions/${id}`, body);
      flagSaved();
      refresh();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  if (q.loading && !q.data) return <div className="p-4"><LoadingRows rows={8} /></div>;
  if (q.error && !q.data) {
    return (
      <div className="flex flex-col gap-3 p-4">
        <ErrorState error={q.error.status === 404 ? { message: t("exceptions.d.not_found") } : q.error} onRetry={q.error.status === 404 ? null : q.reload} />
        {onClose && <Button variant="ghost" size="sm" onClick={onClose}>{t("exceptions.d.close")}</Button>}
      </div>
    );
  }
  const ex = q.data;
  if (!ex) return null;
  const s = ex.shipment;
  const resolved = ex.status === "resolved";
  const desc = localDescription(ex, t, locale);
  const facts = evidenceItems(ex.evidence, t, locale, {
    invoiceLink: (v) => <Link className="text-ink-accent hover:underline" to={`/ops/billing/${v}`}>{t("exceptions.ev.open_invoice")}</Link>,
  });
  const people = assignees.data?.items || [];
  const ownerInList = ex.owner && !people.some((p) => p.id === ex.owner.id);
  const route = s ? [s.origin?.name, s.destination?.name].filter(Boolean).join(" → ") : null;

  return (
    <div className="flex flex-col">
      {q.error && <ErrorState className="m-3" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
      <header className="flex flex-col gap-2 px-4 pb-3 pt-4">
        <div className="flex items-start justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <StatusBadge size="sm" group="severity" value={ex.severity} />
            <StatusBadge size="sm" group="exception" value={ex.status} />
            <span className="text-xs text-ink-secondary">{typeLabel(t, ex.type)}</span>
            <span className="text-xs text-ink-tertiary">·</span>
            <DetectedBy value={ex.detected_by} />
          </div>
          {onClose && (
            <Button variant="ghost" size="icon-xs" onClick={onClose} aria-label={t("exceptions.d.close")}><X /></Button>
          )}
        </div>
        <h2 className="text-md font-medium leading-6">{localTitle(ex, t, locale)}</h2>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
          {s ? (
            <Link to={`/ops/shipments/${s.id}`} className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-medium shadow-[inset_0_0_0_0.5px_var(--border-default)] hover:bg-hover">
              {s.reference} <ArrowRight className="size-3 text-ink-tertiary" />
            </Link>
          ) : (
            <span className="text-ink-tertiary">{ex.shipment_reference || t("exceptions.no_shipment")}</span>
          )}
          {ex.company_id ? (
            <Link to={`/ops/customers/${ex.company_id}`} className="text-ink-secondary hover:text-foreground hover:underline">{ex.company_name || s?.customer_name}</Link>
          ) : null}
        </div>
        {s && (
          <p className="text-xs text-ink-tertiary">
            {[route, tEnum(t, "mode", s.mode), tEnum(t, "status", s.status), s.eta ? `ETA ${dateTime(s.eta, locale)}` : null].filter(Boolean).join(" · ")}
          </p>
        )}
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-secondary">
          <span>{t("exceptions.d.detected")} <span className="tabular">{dateTime(ex.created_at, locale)}</span></span>
          <span>{t("exceptions.d.sla_due")} <SlaDue due={ex.sla_due} status={ex.status} now={now} /></span>
          {ex.last_detected_at && ex.detected_by === "rule" && <span className="text-ink-tertiary">{t("exceptions.d.last_seen", { time: relTime(ex.last_detected_at, locale) })}</span>}
        </div>
      </header>

      {canWrite && (
        <div className="flex flex-wrap items-end gap-2 border-t border-[color:var(--border-subtle)] px-4 py-3">
          <label className="flex min-w-[150px] flex-1 flex-col gap-1">
            <span className="text-xs text-ink-tertiary">{t("exceptions.d.owner")}</span>
            <Select value={ex.owner?.id || "none"} disabled={!!busy || resolved}
              onValueChange={(v) => patch("owner", v === "none" ? { unassign: true } : { owner_id: v })}>
              <SelectTrigger aria-label={t("exceptions.d.owner")}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">{t("exceptions.no_owner")}</SelectItem>
                {ownerInList && <SelectItem value={ex.owner.id}>{ex.owner.name}</SelectItem>}
                {people.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}{p.id === user?.id ? ` (${t("exceptions.filter.owner_me").toLowerCase()})` : ""}</SelectItem>)}
              </SelectContent>
            </Select>
          </label>
          <label className="flex w-[130px] flex-col gap-1">
            <span className="text-xs text-ink-tertiary">{t("exceptions.d.severity")}</span>
            <Select value={ex.severity} disabled={!!busy || resolved} onValueChange={(v) => v !== ex.severity && patch("severity", { severity: v })}>
              <SelectTrigger aria-label={t("exceptions.d.severity")}><SelectValue /></SelectTrigger>
              <SelectContent>
                {SEVERITIES.map((k) => <SelectItem key={k} value={k}>{tEnum(t, "severity", k)}</SelectItem>)}
              </SelectContent>
            </Select>
          </label>
          <div className="flex items-center gap-2">
            {ex.status === "open" && (
              <Button variant="outline" onClick={() => patch("status", { status: "in_progress" })} disabled={!!busy}>
                {busy === "status" ? t("state.saving") : t("exceptions.d.start")}
              </Button>
            )}
            {resolved ? (
              <Button variant="outline" onClick={() => patch("status", { status: "open" })} disabled={!!busy}>
                {busy === "status" ? t("state.saving") : t("exceptions.d.reopen")}
              </Button>
            ) : (
              <Button onClick={() => setResolveOpen(true)} disabled={!!busy}>{t("exceptions.d.resolve")}</Button>
            )}
          </div>
          <div className="basis-full empty:hidden">
            <SavedNote show={saved} />
            {error && <ErrorState compact error={error} />}
          </div>
        </div>
      )}

      {resolved && (
        <Block title={t("exceptions.d.resolution")}>
          <p className="whitespace-pre-wrap text-sm">{localResolution(ex.resolution, t) || "—"}</p>
          <p className="mt-1 text-xs text-ink-tertiary">{t("exceptions.d.resolved_by", { time: dateTime(ex.resolved_at, locale), name: resolverName(ex.resolved_by, t) || "—" })}</p>
        </Block>
      )}

      {(desc || facts.length > 0) && (
        <Block title={t("exceptions.ev.title")}>
          {desc && <p className="mb-3 whitespace-pre-wrap text-sm">{desc}</p>}
          {facts.length > 0 && <KV cols={2} items={facts} />}
        </Block>
      )}

      {!(resolved && !ex.ai_assessment) && (
        <AiAssessment ex={ex} canWrite={canWrite && !resolved} onAssessed={refresh} onSeverity={(v) => patch("severity", { severity: v })} />
      )}
      <CustomerMessage ex={ex} canApprove={canApprove} onChanged={refresh} />

      <Block>
        <label className="flex items-start gap-2 text-sm">
          <Switch aria-label={t("exceptions.d.customer_visible")} checked={ex.customer_visible !== false} disabled={!canWrite || busy === "visible"}
            onCheckedChange={(v) => confirmation.confirm(() => patch("visible", { customer_visible: v }, true), `${t("exceptions.d.customer_visible")}: ${t(v ? "review.permission_yes" : "review.permission_no")}`)} className="mt-0.5" />
          <span>
            {t("exceptions.d.customer_visible")}
            <span className="block text-xs text-ink-tertiary">{ex.customer_visible !== false ? t("exceptions.d.customer_visible_hint") : t("exceptions.d.internal_hint")}</span>
          </span>
        </label>
      </Block>

      <Block title={t("exceptions.d.history")}>
        <Timeline events={historyEvents(ex.history, t)} />
        {canWrite && <NoteForm id={ex.id} onDone={refresh} />}
      </Block>

      {confirmation.dialog}
      <ResolveDialog exception={ex} open={resolveOpen} onOpenChange={setResolveOpen} onResolved={refresh} />
    </div>
  );
}
