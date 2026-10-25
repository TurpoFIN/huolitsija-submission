import React, { useEffect, useRef, useState } from "react";
import { Check, Eye, Loader2, Lock, Paperclip, Send, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Segmented } from "@/components/app/FilterBar";
import { ErrorState } from "@/components/app/states";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { ACCEPT, composerDraft, fileSize, MAX_BODY, useAttachments } from "./util";


/**
 * Message composer. Ctrl/Cmd+Enter sends. A failed send keeps the text and files and offers Retry.
 * onSend({ body, attachments: [documentId], visibility }) must throw on failure.
 * target: { shipment_id?, company_id? } for attachment uploads.
 */
export default function Composer({ mode = "customer", draftKey, target, onSend, placeholder, disabledReason, className, rows = 3, autoFocus }) {
  const { t, locale } = useT();
  const [text, setText] = useState(() => (draftKey ? composerDraft.get(draftKey) : ""));
  const [visibility, setVisibility] = useState("customer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [sentAt, setSentAt] = useState(null);
  const att = useAttachments();
  const fileRef = useRef(null);
  const areaRef = useRef(null);
  const [over, setOver] = useState(false);
  const internal = mode === "ops" && visibility === "internal";

  useEffect(() => {
    if (draftKey) composerDraft.set(draftKey, text);
  }, [draftKey, text]);
  useEffect(() => {
    if (!sentAt) return undefined;
    const id = setTimeout(() => setSentAt(null), 4000);
    return () => clearTimeout(id);
  }, [sentAt]);
  useEffect(() => {
    if (autoFocus) areaRef.current?.focus();
  }, [autoFocus]);

  const empty = !text.trim() && att.files.length === 0;
  const tooLong = text.length > MAX_BODY;
  const blocked = disabledReason || (empty ? t("messages.composer.empty_reason") : null);

  const send = async () => {
    if (busy || disabledReason || empty || tooLong) return;
    setBusy(true);
    setError(null);
    try {
      const ids = await att.uploadAll(target || {});
      await onSend({ body: text.trim(), attachments: ids, visibility: mode === "ops" ? visibility : "customer" });
      setText("");
      att.clear();
      if (draftKey) composerDraft.set(draftKey, "");
      setSentAt(Date.now());
    } catch (e) {
      setError(e?.status === 0 ? t("state.offline") : e?.message || t("state.error"));
    } finally {
      setBusy(false);
    }
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      send();
    }
  };

  const sendLabel = internal ? t("messages.composer.send_note") : t("messages.composer.send");
  const status = att.progress
    ? t("messages.composer.uploading", att.progress)
    : busy ? t("messages.composer.sending") : null;

  return (
    <div className={cn("flex flex-col gap-2", className)}
      onDragOver={(e) => { if (e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); setOver(true); } }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { if (e.dataTransfer?.files?.length) { e.preventDefault(); setOver(false); att.add(e.dataTransfer.files); } }}>
      {mode === "ops" && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <Segmented value={visibility} onChange={setVisibility} label={t("messages.composer.visibility")}
            options={[{ value: "customer", label: t("messages.composer.to_customer") }, { value: "internal", label: t("messages.composer.internal") }]} />
          <span className={cn("items-center gap-1 text-xs text-ink-secondary", internal ? "inline-flex" : "hidden sm:inline-flex")}>
            {internal ? <Lock className="size-3.5" aria-hidden /> : <Eye className="size-3.5" aria-hidden />}
            {internal ? t("messages.composer.visible_internal") : t("messages.composer.visible_customer")}
          </span>
        </div>
      )}
      <Textarea ref={areaRef} value={text} onChange={(e) => setText(e.target.value)} onKeyDown={onKeyDown} rows={rows}
        aria-label={t("messages.composer.label")} disabled={!!disabledReason}
        placeholder={placeholder || (internal ? t("messages.composer.placeholder_internal") : mode === "ops" ? t("messages.composer.placeholder_ops") : t("messages.composer.placeholder_customer"))}
        className={cn("min-h-[64px] resize-y", internal && "border border-dashed border-[color:var(--border-strong)] bg-[var(--bg-subtle)]", over && "bg-hover")} />
      {att.files.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label={t("messages.row.attachment")}>
          {att.files.map((f) => (
            <li key={f.key} className="inline-flex h-6 max-w-full items-center gap-1 rounded-full bg-secondary pl-2 pr-1 text-xs">
              {f.docId ? <Check className="size-3 shrink-0 text-success-fg" aria-label={t("messages.composer.uploaded")} /> : <Paperclip className="size-3 shrink-0 text-ink-tertiary" aria-hidden />}
              <span className="max-w-[180px] truncate">{f.file.name}</span>
              <span className="tabular text-ink-tertiary">{fileSize(f.file.size, locale)}</span>
              <button type="button" disabled={busy} onClick={() => att.remove(f.key)} aria-label={t("messages.composer.remove_file", { name: f.file.name })}
                className="rounded-full p-0.5 hover:bg-hover disabled:opacity-50"><X className="size-3" /></button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <input ref={fileRef} type="file" multiple accept={ACCEPT} className="sr-only" tabIndex={-1} aria-hidden
          onChange={(e) => { att.add(e.target.files); e.target.value = ""; }} />
        <Button variant="ghost" size="sm" onClick={() => fileRef.current?.click()} disabled={busy || !!disabledReason}
          aria-label={t("messages.composer.attach")} title={t("messages.composer.attach")}>
          <Paperclip /> <span className="hidden sm:inline">{t("messages.composer.attach")}</span>
        </Button>
        <span className="ml-auto flex min-w-0 items-center gap-2 text-xs text-ink-tertiary">
          {status ? <span aria-live="polite">{status}</span>
            : sentAt ? <span className="inline-flex items-center gap-1 text-success-fg" aria-live="polite"><Check className="size-3.5" />{t("messages.composer.sent")}</span>
            : tooLong ? <span className="text-danger-fg tabular">{text.length} / {MAX_BODY}</span>
            : <span className="hidden sm:inline">{t("messages.composer.shortcut")}</span>}
        </span>
        <span title={blocked || undefined}>
          <Button size="sm" onClick={send} disabled={busy || !!blocked || tooLong} aria-label={sendLabel}>
            {busy ? <Loader2 className="animate-spin" /> : <Send />} {sendLabel}
          </Button>
        </span>
      </div>
      {disabledReason && <p className="text-xs text-ink-tertiary">{disabledReason}</p>}
      {error && <ErrorState compact error={{ message: t("messages.composer.failed", { error }) }} onRetry={send} />}
    </div>
  );
}
