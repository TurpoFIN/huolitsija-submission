import React, { useEffect, useRef, useState } from "react";
import { Check, ChevronRight, Loader2, Send, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiPatch, apiPost } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { MAX_BODY, messageTime, pct } from "./util";

/**
 * Ops card for a message waiting for approval (AI reply draft, exception notice or routine update).
 * Edit -> Save (PATCH), "Hyväksy ja lähetä" (approve with the edited text), "Hylkää" (optional note).
 * Nothing here reaches the customer until a person approves.
 */
export default function AiDraftCard({ m, onDecided }) {
  const { t, locale } = useT();
  const [text, setText] = useState(m.body || "");
  const [saved, setSaved] = useState(m.body || "");
  const [busy, setBusy] = useState(null); // "save" | "approve" | "reject"
  const [justSaved, setJustSaved] = useState(false);
  const [error, setError] = useState(null);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const serverBody = useRef(m.body || "");

  // A poll may bring a newer server body (someone else saved): adopt it only when there are no local edits.
  useEffect(() => {
    if ((m.body || "") === serverBody.current) return;
    const prev = serverBody.current;
    serverBody.current = m.body || "";
    setSaved(m.body || "");
    setText((cur) => (cur === prev ? m.body || "" : cur));
  }, [m.body]);

  const ai = m.ai || {};
  const dirty = text.trim() !== (saved || "").trim();
  const routine = m.kind === "update" || ai.routine;
  const title = routine ? t("messages.ai.routine_title") : ai.agent === "exception_agent" ? t("messages.ai.exception_title") : t("messages.ai.title");
  const facts = Array.isArray(ai.facts_used) ? ai.facts_used : [];
  const lines = Math.min(14, Math.max(4, text.split("\n").length + Math.ceil(text.length / 90)));

  const fail = async (e) => {
    if (e?.status === 409) {
      setError(t("messages.ai.already_decided"));
      await onDecided?.();
      return;
    }
    setError(t("messages.ai.failed", { error: e?.status === 0 ? t("state.offline") : e?.message || t("state.error") }));
  };

  const save = async () => {
    if (!text.trim()) return;
    setBusy("save");
    setError(null);
    try {
      const r = await apiPatch(`/messages/${m.id}`, { body: text.trim() });
      serverBody.current = r?.body ?? text.trim();
      setSaved(serverBody.current);
      setJustSaved(true);
    } catch (e) {
      await fail(e);
    } finally {
      setBusy(null);
    }
  };

  const approve = async () => {
    if (!text.trim()) return;
    setBusy("approve");
    setError(null);
    try {
      await apiPost(`/messages/${m.id}/approve`, { body: text.trim() });
      await onDecided?.("approved");
    } catch (e) {
      await fail(e);
    } finally {
      setBusy(null);
    }
  };

  const reject = async () => {
    setBusy("reject");
    setError(null);
    try {
      await apiPost(`/messages/${m.id}/reject`, { note: note.trim() || null });
      await onDecided?.("rejected");
    } catch (e) {
      await fail(e);
    } finally {
      setBusy(null);
    }
  };

  useEffect(() => {
    if (dirty) setJustSaved(false);
  }, [dirty]);

  const emptyReason = !text.trim() ? t("messages.ai.empty_body") : null;

  return (
    <section aria-label={title} data-draft-id={m.id}
      className="w-full max-w-[680px] self-end surface-flat rounded-xl p-3">
      <header className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <AgentMark />
        <h3 className="text-sm font-medium">{title}</h3>
        {typeof ai.confidence === "number" && (
          <span className="text-xs tabular text-ink-secondary">{t("messages.ai.confidence", { pct: pct(ai.confidence, locale) })}</span>
        )}
        {ai.needs_human && <StatusBadge size="sm" state="pending" label={t("messages.ai.needs_human")} />}
        <span className="ml-auto text-xs text-ink-tertiary">
          {t("messages.ai.drafted_at", { time: messageTime(ai.drafted_at || m.created_at, locale) })}
          {ai.requested_by?.name ? ` · ${t("messages.ai.requested_by", { name: ai.requested_by.name })}` : ""}
        </span>
      </header>
      <p className="mt-1 flex items-center gap-1.5 text-xs text-ink-secondary">
        <ShieldCheck className="size-3.5 shrink-0" aria-hidden />
        {t("messages.ai.gate")}
      </p>
      {ai.reason && (
        <p className="mt-2 text-xs text-ink-secondary">
          <span className="text-ink-tertiary">{t("messages.ai.reason")}: </span>{ai.reason}
        </p>
      )}
      {facts.length > 0 && (
        <details className="group mt-1 text-xs">
          <summary className="inline-flex cursor-pointer list-none items-center gap-1 rounded text-ink-secondary hover:text-foreground [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
            {t("messages.ai.facts", { n: facts.length })}
          </summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-8 text-ink-secondary">
            {facts.map((f, i) => <li key={i} className="break-words">{typeof f === "string" ? f : JSON.stringify(f)}</li>)}
          </ul>
        </details>
      )}
      <Textarea value={text} onChange={(e) => setText(e.target.value)} rows={lines} maxLength={MAX_BODY}
        aria-label={t("messages.ai.draft_label")} className="mt-3 max-h-[60vh] resize-y [field-sizing:content]" disabled={!!busy}
        onKeyDown={(e) => { if (e.key === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (dirty) save(); } }} />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span title={!dirty ? t("messages.ai.saved") : emptyReason || undefined}>
          <Button size="sm" variant="outline" onClick={save} disabled={!dirty || !!busy || !text.trim()}>
            {busy === "save" && <Loader2 className="animate-spin" />}{t("messages.ai.save")}
          </Button>
        </span>
        <span className="text-xs text-ink-tertiary" aria-live="polite">
          {busy === "save" ? t("state.saving")
            : dirty ? t("messages.ai.unsaved")
            : justSaved ? <span className="inline-flex items-center gap-1 text-success-fg"><Check className="size-3.5" />{t("messages.ai.saved")}</span>
            : null}
        </span>
        <div className="ml-auto flex items-center gap-2">
          {!rejecting && (
            <Button size="sm" variant="ghost" onClick={() => setRejecting(true)} disabled={!!busy}>{t("messages.ai.reject")}</Button>
          )}
          <span title={emptyReason || undefined}>
            <Button size="sm" onClick={approve} disabled={!!busy || !text.trim()}>
              {busy === "approve" ? <Loader2 className="animate-spin" /> : <Send />}{t("messages.ai.approve")}
            </Button>
          </span>
        </div>
      </div>
      {rejecting && (
        <div className={cn("mt-2 flex flex-wrap items-center gap-2 rounded-lg bg-[var(--bg-subtle)] p-2")}>
          <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("messages.ai.reject_note")} aria-label={t("messages.ai.reject_note")}
            className="min-w-[180px] flex-1" autoFocus onKeyDown={(e) => { if (e.key === "Enter") reject(); if (e.key === "Escape") setRejecting(false); }} />
          <Button size="sm" variant="destructive" onClick={reject} disabled={!!busy}>
            {busy === "reject" && <Loader2 className="animate-spin" />}{t("messages.ai.reject_confirm")}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => { setRejecting(false); setNote(""); }} disabled={!!busy}>{t("action.cancel")}</Button>
        </div>
      )}
      {error && <ErrorState compact className="mt-2" error={{ message: error }} />}
    </section>
  );
}
