// Document review sheet (?doc=<id>): file preview left, extracted values right with provenance and confidence,
// inline edit, Confirm (PATCH confirm:true), DOC-3 cross-document checks, re-read, type change, delete.
import React, { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Check, CheckCircle2, Download, ExternalLink, Loader2, PencilLine, Plus, RotateCw, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Segmented } from "@/components/app/FilterBar";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { Skeleton } from "@/components/ui/skeleton";
import { apiDelete, apiPatch, apiPost, fileUrl, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, num, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Confidence, ProvenanceChip } from "@/features/documents/Provenance";
import { DocStatus } from "@/features/documents/DocumentTable";
import { useSpecs } from "@/features/documents/UploadBox";
import {
  LOW_CONFIDENCE, changedFields, fileSize, formatValue, groupFields, isNumeric, isReading, isSheet, localized, pctText, reviewReason, tn, toInput,
} from "@/features/documents/fields";

const empty = (v) => v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length);

function Preview({ doc }) {
  const { t } = useT();
  const [failed, setFailed] = useState(false);
  const pages = Math.max(1, doc.pages || 1);
  const file = fileUrl(`/documents/${doc.id}/file`);
  if (isSheet(doc)) {
    return (
      <div className="flex flex-col items-start gap-3 rounded-xl border border-dashed bg-surface px-5 py-8 text-sm">
        <p className="text-ink-secondary">{t("doc.review.no_preview_sheet")}</p>
        <Button asChild variant="outline"><a href={fileUrl(`/documents/${doc.id}/file?download=true`)}><Download /> {t("doc.review.download")}</a></Button>
      </div>
    );
  }
  if (failed) {
    return (
      <div className="flex h-full flex-col gap-2">
        <p className="text-xs text-ink-tertiary">
          {t("doc.review.no_preview")} <a href={file} target="_blank" rel="noreferrer" className="text-ink-accent hover:underline">{t("doc.review.open_pdf")}</a>
        </p>
        <iframe title={doc.filename} src={file} className="min-h-[70vh] w-full flex-1 rounded-md bg-white" />
      </div>
    );
  }
  return (
    <div className="mx-auto flex max-w-[860px] flex-col gap-4">
      {Array.from({ length: pages }).map((_, i) => (
        <figure key={i} className="flex flex-col gap-1">
          {pages > 1 && <figcaption className="text-xs text-ink-tertiary">{t("doc.review.page", { n: i + 1, total: pages })}</figcaption>}
          <img src={fileUrl(`/documents/${doc.id}/preview?page=${i + 1}&width=1200`)} alt={i === 0 ? doc.filename : ""} loading={i < 2 ? "eager" : "lazy"}
            onError={() => setFailed(true)} className="w-full rounded-md bg-white shadow-sm ring-1 ring-black/10" />
        </figure>
      ))}
    </div>
  );
}

function GroupTitle({ children, aside }) {
  return (
    <div className="mb-1 mt-6 flex items-center justify-between gap-2">
      <h3 className="text-xs font-medium uppercase tracking-wide text-ink-tertiary">{children}</h3>
      {aside}
    </div>
  );
}

function FieldRow({ spec, field, editing, value, onChange, warnings = [] }) {
  const { t, locale } = useT();
  const v = field?.value;
  const missing = empty(v);
  const low = field && field.confidence !== null && field.confidence !== undefined && field.confidence < LOW_CONFIDENCE;
  const id = `doc-f-${spec.key}`;
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1 border-b border-[color:var(--border-subtle)] py-1.5 sm:grid-cols-[11rem_minmax(0,1fr)_auto] sm:items-center">
      <label htmlFor={editing ? id : undefined} className="self-center text-sm text-ink-secondary">
        {t(`doc.field.${spec.key}`)}
        {spec.required && <span className="ml-0.5 text-ink-tertiary" title={t("doc.review.required")}>*</span>}
      </label>
      <div className={cn("col-span-2 row-start-2 min-w-0 sm:col-span-1 sm:col-start-2 sm:row-start-1", isNumeric(spec.kind) && !editing && "text-right")}>
        {editing ? (
          <Input id={id} value={value ?? ""} onChange={(e) => onChange(e.target.value)} aria-invalid={warnings.some((w) => w.severity === "error") || undefined}
            inputMode={isNumeric(spec.kind) ? "decimal" : undefined} placeholder={spec.kind === "date" ? "YYYY-MM-DD" : undefined}
            className={cn(isNumeric(spec.kind) && "text-right tabular")} />
        ) : missing ? (
          <span className={cn("text-sm", spec.required ? "text-danger-fg" : "text-ink-tertiary")}>
            —{spec.required && <span className="ml-1.5 text-xs">{t("doc.review.missing_required")}</span>}
          </span>
        ) : (
          <span className={cn("text-sm break-words", isNumeric(spec.kind) && "tabular", /eori|vat|hs/.test(spec.kind) && "font-mono text-[13px]")}>
            {formatValue(spec.kind, v, locale, spec.key)}
          </span>
        )}
        {editing && spec.kind === "list" && <p className="mt-0.5 text-xs text-ink-tertiary">{t("doc.review.list_hint")}</p>}
        {!editing && low && <p className="mt-0.5 text-xs text-warning-fg">{t("doc.review.low", { pct: pctText(field.confidence, locale) })}</p>}
        {warnings.map((w, i) => (
          <p key={i} className={cn("mt-0.5 text-xs", w.severity === "error" ? "text-danger-fg" : w.severity === "warning" ? "text-warning-fg" : "text-ink-tertiary")}>{w.message}</p>
        ))}
      </div>
      <div className="col-start-2 row-start-1 flex items-center justify-end gap-1.5 sm:col-start-3">
        {field && !missing && <ProvenanceChip source={field.source} />}
        {field && !missing && <Confidence value={field.confidence} className="w-9 justify-end" />}
      </div>
    </div>
  );
}

function LineItems({ spec, items, source, editing, lines, setLines, fields }) {
  const { t, locale } = useT();
  const rows = editing ? lines : items;
  const cols = editing ? spec : spec.filter((c) => items.some((it) => !empty(it[c.key])));
  const sum = items.reduce((a, it) => a + (Number(it.amount) || 0), 0);
  const subtotal = fields?.subtotal_amount?.value ?? fields?.total_amount?.value;
  const currency = fields?.currency?.value;
  const hasAmounts = items.some((it) => !empty(it.amount));
  const off = hasAmounts && subtotal !== undefined && subtotal !== null && Math.abs(sum - Number(subtotal)) > 0.01;
  const setCell = (i, k, v) => setLines((ls) => ls.map((l, j) => (j === i ? { ...l, [k]: v } : l)));
  return (
    <>
      <GroupTitle aside={<span className="text-xs text-ink-tertiary tabular">{tn(t, "doc.review.lines_n", rows.length)}</span>}>{t("doc.review.lines")}</GroupTitle>
      {rows.length === 0 && !editing ? (
        <p className="py-2 text-sm text-ink-tertiary">—</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-[color:var(--border-subtle)]">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[color:var(--border-subtle)] text-xs text-ink-tertiary">
                {cols.map((c) => (
                  <th key={c.key} scope="col" className={cn("whitespace-nowrap px-2 py-1.5 font-medium", isNumeric(c.kind) ? "text-right" : "text-left")}>{t(`doc.li.${c.key}`)}</th>
                ))}
                {!editing && <th scope="col" className="px-2 py-1.5 text-right font-medium">{t("doc.li.confidence")}</th>}
                {editing && <th scope="col"><span className="sr-only">{t("doc.review.remove_line")}</span></th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((it, i) => (
                <tr key={i} className="border-b border-[color:var(--border-subtle)] last:border-0 align-top">
                  {cols.map((c) => (
                    <td key={c.key} className={cn("px-2 py-1.5", isNumeric(c.kind) ? "text-right tabular whitespace-nowrap" : c.key === "description" ? "min-w-[14rem]" : "whitespace-nowrap",
                      /hs|country/.test(c.kind) && "font-mono text-[13px]")}>
                      {editing ? (
                        <Input value={it[c.key] ?? ""} onChange={(e) => setCell(i, c.key, e.target.value)} aria-label={`${t(`doc.li.${c.key}`)} ${i + 1}`}
                          className={cn(c.key === "description" ? "min-w-[14rem]" : "min-w-[5.5rem]", isNumeric(c.kind) && "text-right tabular")} />
                      ) : formatValue(c.kind, it[c.key], locale, c.key)}
                    </td>
                  ))}
                  {!editing && (
                    <td className="px-2 py-1.5 text-right">
                      <span className="inline-flex items-center gap-1.5">
                        <ProvenanceChip source={it.source || source} />
                        <Confidence value={it.confidence} />
                      </span>
                    </td>
                  )}
                  {editing && (
                    <td className="px-1 py-1.5">
                      <Button size="icon" variant="ghost" onClick={() => setLines((ls) => ls.filter((_, j) => j !== i))} aria-label={t("doc.review.remove_line")}><X /></Button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && (
        <Button variant="ghost" className="mt-1" onClick={() => setLines((ls) => [...ls, {}])}><Plus /> {t("doc.review.add_line")}</Button>
      )}
      {!editing && hasAmounts && (
        <p className={cn("mt-1.5 text-right text-xs tabular", off ? "text-warning-fg" : "text-ink-tertiary")}>
          {t("doc.review.lines_sum", { sum: `${formatValue("number", sum, locale, "amount")}${currency ? ` ${currency}` : ""}` })}
        </p>
      )}
    </>
  );
}

function checkValue(v, locale) {
  if (empty(v)) return "—";
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "number") return num(v, locale, 3);
  return String(v);
}

function Checks({ checks }) {
  const { t, locale } = useT();
  return (
    <>
      <GroupTitle>{t("doc.checks.title")}</GroupTitle>
      <p className="mb-1 text-xs text-ink-tertiary">{t("doc.checks.rule")}</p>
      {checks.length === 0 ? (
        <p className="py-1.5 text-sm text-ink-tertiary">{t("doc.checks.none")}</p>
      ) : (
        <ul>
          {checks.map((c) => (
            <li key={c.key} className="border-b border-[color:var(--border-subtle)] py-1.5 last:border-0">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm">{t(`doc.check.${c.key}`)}</span>
                <StatusBadge size="sm" state={c.ok === true ? "success" : c.ok === false ? "failed" : "expired"}
                  label={t(c.ok === true ? "doc.checks.ok" : c.ok === false ? "doc.checks.fail" : "doc.checks.na")} />
              </div>
              {c.ok === false && c.message && <p className="mt-0.5 text-xs text-danger-fg">{localized(c, "message", locale)}</p>}
              {c.values?.length > 0 && (
                <p className="mt-0.5 text-xs text-ink-tertiary">
                  {c.values.map((v, i) => (
                    <span key={i} className="mr-3 inline-block whitespace-nowrap">
                      {tEnum(t, "doc", v.type)} <span className="tabular text-ink-secondary">{checkValue(v.value, locale)}</span>
                    </span>
                  ))}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function Warnings({ warnings }) {
  const { t } = useT();
  const shown = warnings.filter((w) => w.severity !== "info" || !w.field);
  if (!shown.length) {
    return <p className="flex items-center gap-1.5 text-sm text-ink-tertiary"><CheckCircle2 className="size-4 text-success-fg" aria-hidden />{t("doc.warn.none")}</p>;
  }
  return (
    <>
      <GroupTitle>{t("doc.warn.title")}</GroupTitle>
      <ul>
        {shown.map((w, i) => (
          <li key={i} className="flex items-start gap-2 border-b border-[color:var(--border-subtle)] py-1.5 text-sm last:border-0">
            <StatusBadge size="sm" group="generic" value={w.severity} icon={w.severity !== "info"} label={t(`doc.warn.${w.severity}`)} className="mt-px" />
            <span className="min-w-0 flex-1">
              {w.message}
              {w.field && <span className="ml-1.5 text-xs text-ink-tertiary">{t(`doc.field.${w.field}`)}</span>}
              {w.code && <span className="ml-1.5 font-mono text-[11px] text-ink-tertiary">{w.code}</span>}
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

function ReviewBody({ id, onClose, onChanged, base }) {
  const { t, locale } = useT();
  const { user, can } = useAuth();
  const specs = useSpecs();
  const [poll, setPoll] = useState(false);
  const q = useApi(`/documents/${id}`, { pollMs: poll ? 2500 : undefined });
  const doc = q.data;
  const ex = doc?.extraction || {};
  const status = ex.status;
  const reading = isReading(status);
  const [pane, setPane] = useState("values");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({});
  const [lines, setLines] = useState([]);
  const [save, setSave] = useState({ state: "idle" });
  const [busy, setBusy] = useState(null);
  const [actErr, setActErr] = useState(null);
  const wasReading = useRef(false);

  useEffect(() => {
    setPoll(reading);
    if (wasReading.current && !reading && status) onChanged?.();
    wasReading.current = reading;
  }, [reading, status]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (save.state !== "saved") return undefined;
    const tm = setTimeout(() => setSave({ state: "idle" }), 4000);
    return () => clearTimeout(tm);
  }, [save.state]);

  const spec = doc && specs ? specs.fields?.[doc.type] || specs.fields?.other : null;
  const extractable = doc && specs ? specs.extractable?.includes(doc.type) : false;
  const canEdit = user?.org_type === "internal" || can("company.book") || can("company.manage");
  const fields = ex.fields || {};
  const items = ex.line_items || [];
  const warnings = ex.warnings || [];
  const byField = useMemo(() => {
    const m = {};
    (ex.warnings || []).forEach((w) => { if (w.field) (m[w.field] = m[w.field] || []).push(w); });
    return m;
  }, [ex.warnings]);
  const groups = useMemo(() => groupFields(spec?.fields || []), [spec]);
  const hasValues = Object.keys(fields).length > 0 || items.length > 0;
  const showFields = spec && !reading && (status !== "not_applicable" || hasValues);

  const startEdit = () => {
    setDraft(Object.fromEntries((spec?.fields || []).map((f) => [f.key, toInput(fields[f.key]?.value)])));
    setLines(items.map((it) => Object.fromEntries((spec?.line_items || []).map((c) => [c.key, toInput(it[c.key])]))));
    setEditing(true);
    setSave({ state: "idle" });
    setPane("values");
  };

  const linesPayload = () => lines.filter((l) => Object.values(l).some((v) => String(v ?? "").trim()))
    .map((l) => Object.fromEntries(Object.entries(l).map(([k, v]) => [k, String(v ?? "").trim()])));
  const linesChanged = () => {
    const before = items.map((it) => Object.fromEntries((spec?.line_items || []).map((c) => [c.key, toInput(it[c.key])])));
    return JSON.stringify(linesPayload()) !== JSON.stringify(before.filter((l) => Object.values(l).some((v) => v !== "")));
  };

  const submit = async (confirm) => {
    const body = {};
    if (editing) {
      const ch = changedFields(draft, fields, spec?.fields || []);
      if (Object.keys(ch).length) body.fields = ch;
      if (linesChanged()) body.line_items = linesPayload();
    }
    if (confirm) body.confirm = true;
    if (!Object.keys(body).length) { setEditing(false); return; }
    setSave({ state: "saving" });
    try {
      const d = await apiPatch(`/documents/${id}`, body);
      q.setData(d);
      setEditing(false);
      setSave({ state: "saved", confirmed: !!confirm });
      onChanged?.();
    } catch (e) {
      setSave({ state: "error", message: e.message });
    }
  };

  const act = async (name, fn) => {
    setBusy(name);
    setSave({ state: "idle" });
    setActErr(null);
    try {
      await fn();
    } catch (e) {
      setActErr(e.message);
    } finally {
      setBusy(null);
    }
  };
  const reextract = () => act("reextract", async () => {
    await apiPost(`/documents/${id}/reextract`, {});
    setEditing(false);
    await q.reload();
    setPoll(true);
    onChanged?.();
  });
  const changeType = (ty) => act("type", async () => {
    const d = await apiPatch(`/documents/${id}`, { type: ty });
    q.setData(d);
    setEditing(false);
    onChanged?.();
  });
  const remove = () => act("delete", async () => {
    await apiDelete(`/documents/${id}`);
    onChanged?.();
    onClose();
  });

  if (q.loading && !doc) {
    return (
      <>
        <SheetTitle className="sr-only">{t("doc.review.title")}</SheetTitle>
        <div className="border-b px-5 py-4"><Skeleton className="h-5 w-48" /><Skeleton className="mt-2 h-4 w-72" /></div>
        <div className="grid flex-1 gap-4 p-5 lg:grid-cols-2"><Skeleton className="hidden h-full min-h-[60vh] lg:block" /><LoadingRows rows={10} /></div>
      </>
    );
  }
  if (q.error && !doc) {
    return (
      <div className="p-5 pr-12">
        <SheetTitle className="mb-3 text-md font-medium">{t("doc.review.title")}</SheetTitle>
        <ErrorState error={q.error.status === 404 ? { message: t("doc.review.not_found") } : q.error} onRetry={q.error.status === 404 ? undefined : q.reload} />
      </div>
    );
  }
  if (!doc) return null;

  const canReextract = canEdit && !reading && (["failed", "needs_review"].includes(status) || (status === "not_applicable" && extractable));
  const decl = (doc.declarations || [])[0];
  const model = ex.model ? String(ex.model).split("/").pop() : null;

  return (
    <>
      <header className="border-b px-4 pb-3 pt-3.5 pr-12 sm:px-5">
        <div className="flex flex-wrap items-center gap-2">
          <SheetTitle className="text-md font-medium">{tEnum(t, "doc", doc.type)}</SheetTitle>
          <DocStatus doc={doc} size="default" />
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-sm text-ink-secondary">
          <span className="max-w-[22rem] truncate text-foreground" title={doc.filename}>{doc.filename}</span>
          {doc.number && <span className="tabular">{doc.number}</span>}
          {doc.shipment_id && (
            <Link to={`${base}/shipments/${doc.shipment_id}`} className="tabular hover:underline underline-offset-4">{doc.shipment_ref || doc.shipment_id.slice(0, 8)}</Link>
          )}
          {decl && (
            <Link to={`${base}/customs/${decl.id}`} className="hover:underline underline-offset-4">{t("doc.review.used_by", { lrn: decl.lrn })}</Link>
          )}
          <span className="text-ink-tertiary">
            {[doc.pages > 1 ? t("doc.review.pages", { n: doc.pages }) : null, fileSize(doc.size, locale)].filter(Boolean).join(" · ")}
          </span>
          <span className="text-ink-tertiary" title={dateTime(doc.uploaded_at, locale)}>
            {t("doc.review.uploaded_by", { name: doc.uploaded_by?.name || "—", time: relTime(doc.uploaded_at, locale) })}
          </span>
        </div>
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <Button asChild variant="outline" className="max-sm:w-8 max-sm:px-0">
            <a href={fileUrl(`/documents/${id}/file`)} target="_blank" rel="noreferrer" aria-label={t("doc.review.open_pdf")}>
              <ExternalLink /> <span className="max-sm:hidden">{t("doc.review.open_pdf")}</span>
            </a>
          </Button>
          <Button asChild variant="outline" className="max-sm:w-8 max-sm:px-0">
            <a href={fileUrl(`/documents/${id}/file?download=true`)} aria-label={t("doc.review.download")}>
              <Download /> <span className="max-sm:hidden">{t("doc.review.download")}</span>
            </a>
          </Button>
          {canReextract && (
            <Button variant="outline" onClick={reextract} disabled={!!busy}>
              {busy === "reextract" ? <Loader2 className="animate-spin" /> : <RotateCw />} {t("doc.review.reextract")}
            </Button>
          )}
          {canEdit && specs && (
            <Select value={doc.type} onValueChange={changeType} disabled={!!busy || reading}>
              <SelectTrigger className="w-auto min-w-[9rem] rounded-full sm:min-w-[11rem]" aria-label={t("doc.review.change_type")}><SelectValue /></SelectTrigger>
              <SelectContent>
                {specs.types.map((ty) => <SelectItem key={ty} value={ty}>{tEnum(t, "doc", ty)}</SelectItem>)}
              </SelectContent>
            </Select>
          )}
          {doc.can_delete && (
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button variant="ghost" className="text-danger-fg" disabled={!!busy}><Trash2 /> {t("doc.review.delete")}</Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>{t("doc.review.delete_title")}</AlertDialogTitle>
                  <AlertDialogDescription>{doc.filename} — {t("doc.review.delete_body")}</AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>{t("action.cancel")}</AlertDialogCancel>
                  <AlertDialogAction onClick={remove} className="bg-destructive/10 text-danger-fg hover:bg-destructive/20">{t("doc.review.delete")}</AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          )}
        </div>
        {actErr && <ErrorState compact className="mt-2" error={{ message: actErr }} />}
      </header>

      <div className="border-b px-4 py-2 lg:hidden">
        <Segmented value={pane} onChange={setPane} label={t("doc.review.title")}
          options={[{ value: "values", label: t("doc.review.fields") }, { value: "preview", label: t("doc.review.preview") }]} />
      </div>

      <div className="grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)] overflow-hidden lg:grid-cols-2">
        <div data-pane="preview" className={cn("min-h-0 overflow-y-auto bg-subtle p-4", pane !== "preview" && "hidden lg:block")} aria-label={t("doc.review.preview")}>
          <Preview doc={doc} />
        </div>
        <div data-pane="values" className={cn("min-h-0 overflow-y-auto px-4 py-4 sm:px-5", pane !== "values" && "hidden lg:block")}>
          {q.error && <ErrorState compact className="mb-3" error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}

          {reading && (
            <div className="mb-4 flex items-start gap-3 rounded-lg px-3 py-2.5 text-sm status-in-progress" role="status">
              <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin" aria-hidden />
              <div><p className="font-medium">{t("doc.review.reading")}</p><p className="text-xs opacity-80">{t("doc.review.reading_hint")}</p></div>
            </div>
          )}
          {!reading && ex.reason && ["needs_review", "failed"].includes(status) && (
            <div className={cn("mb-4 rounded-lg px-3 py-2.5 text-sm", status === "failed" ? "status-failed" : "status-in-review")}>
              <p className="font-medium">{t("doc.review.reason")}</p>
              <p className="mt-0.5">{reviewReason(ex, t)}</p>
            </div>
          )}
          {ex.confirmed_by && !editing && (
            <p className="mb-3 flex items-center gap-1.5 text-sm text-success-fg">
              <CheckCircle2 className="size-4" aria-hidden />
              {t("doc.review.confirmed_by", { name: ex.confirmed_by.name || "—", time: relTime(ex.confirmed_at, locale) })}
            </p>
          )}
          {!reading && ex.source === "ai" && model && (
            <p className="mb-3 flex flex-wrap items-center gap-1.5 text-xs text-ink-tertiary">
              <AgentMark label={t("doc.prov.ai")} title={t("doc.prov.ai_title")} />
              {t("doc.review.model", { model, method: t(`doc.review.method.${ex.method || "text"}`) })}
              {ex.detected_type && ex.detected_type !== doc.type && <span>· {t("doc.review.detected", { type: tEnum(t, "doc", ex.detected_type) })}</span>}
            </p>
          )}
          {!reading && ex.source === "seed" && user?.org_type === "internal" && <p className="mb-3 text-xs text-ink-tertiary">{t("doc.review.seed_note")}</p>}

          {status === "not_applicable" && !hasValues && (
            <p className="mb-4 text-sm text-ink-secondary">{t("doc.review.not_extractable")}</p>
          )}

          {showFields && !editing && <div className="mb-2"><Warnings warnings={warnings} /></div>}

          {doc.shipment_id && !reading && status !== "not_applicable" && <Checks checks={doc.checks || []} />}

          {showFields && (
            <>
              {!hasValues && !editing && <p className="mt-5 text-sm text-ink-tertiary">{t("doc.review.no_fields")}</p>}
              {groups.map((g) => (
                <div key={g.group}>
                  <GroupTitle>{t(`doc.review.group.${g.group}`)}</GroupTitle>
                  {g.fields.map((f) => (
                    <FieldRow key={f.key} spec={f} field={fields[f.key]} editing={editing} value={draft[f.key]} warnings={byField[f.key]}
                      onChange={(v) => setDraft((d) => ({ ...d, [f.key]: v }))} />
                  ))}
                </div>
              ))}
              {(spec.line_items || []).length > 0 && (
                <LineItems spec={spec.line_items} items={items} source={ex.source} editing={editing} lines={lines} setLines={setLines} fields={fields} />
              )}
            </>
          )}
          {!specs && !reading && <LoadingRows rows={8} />}
        </div>
      </div>

      {canEdit && showFields && (
        <footer className="flex flex-wrap items-center justify-end gap-2 border-t px-4 py-3 sm:px-5">
          <div className="mr-auto min-w-0 basis-full text-sm sm:basis-auto" aria-live="polite">
            {save.state === "saving" && <span className="inline-flex items-center gap-1.5 text-ink-secondary"><Loader2 className="size-4 animate-spin" />{t("doc.review.saving")}</span>}
            {save.state === "saved" && <span className="inline-flex items-center gap-1.5 text-success-fg"><Check className="size-4" />{t("doc.review.saved")}</span>}
            {save.state === "error" && <span className="text-danger-fg">{t("doc.err.save", { reason: save.message })}</span>}
            {save.state === "idle" && !ex.confirmed_by && <span className="text-xs text-ink-tertiary">{t("doc.review.confirm_hint")}</span>}
          </div>
          {editing ? (
            <>
              <Button variant="ghost" onClick={() => { setEditing(false); setSave({ state: "idle" }); }}>{t("doc.review.cancel_edit")}</Button>
              <Button variant="outline" onClick={() => submit(false)} disabled={save.state === "saving"}>{t("doc.review.save")}</Button>
              <Button onClick={() => submit(true)} disabled={save.state === "saving"}><Check /> {t("doc.review.confirm")}</Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={startEdit} disabled={!!busy}><PencilLine /> {t("doc.review.edit")}</Button>
              {!ex.confirmed_by && (
                <Button onClick={() => submit(true)} disabled={save.state === "saving" || !!busy}><Check /> {t("doc.review.confirm")}</Button>
              )}
            </>
          )}
        </footer>
      )}
    </>
  );
}

/** Right-side sheet; full screen below 640px with a Values / Preview switch below 1024px. */
export default function DocumentReview({ docId, onClose, onChanged, base = "/portal" }) {
  return (
    <Sheet open={!!docId} onOpenChange={(o) => { if (!o) onClose(); }}>
      <SheetContent side="right" aria-describedby={undefined}
        className="flex w-full flex-col gap-0 p-0 sm:max-w-none md:w-[min(760px,100vw)] lg:w-[min(1320px,96vw)]">
        {docId && <ReviewBody key={docId} id={docId} onClose={onClose} onChanged={onChanged} base={base} />}
      </SheetContent>
    </Sheet>
  );
}
