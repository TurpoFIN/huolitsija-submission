// Ops quote review: approve / price / send. Decision: is this quote ready to go to the customer, at what margin?
import React, { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { FileText, Plus, RefreshCw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DateInput } from "@/components/ui/DateInput";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { KV, PageHeader, Section } from "@/components/app/layout";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPatch, apiPost, fileUrl, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { daysUntil, dateTime, num, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Field, MODES, NativeSelect, NumInput, cargoText, eurTotal, isBlank, locCode, locName } from "@/features/quotes/ops/common";
import SourcingStatus from "@/features/quotes/SourcingStatus";
import { LinesEditor, OptionEditor, Warnings, hasDgLine, initDraft } from "@/features/quotes/ops/OptionEditor";
import { approvalReasons, cleanLine, optionTotals } from "@/features/quotes/ops/quoteMath";
import { dateNum } from "@/features/quotes/ops/RatesTable";

import EmailPanel from "@/features/email/Thread";

const LOCKED = ["accepted", "booked"];
const hhmm = (d, locale) => d.toLocaleTimeString(locale === "fi" ? "fi-FI" : "en-GB", { hour: "2-digit", minute: "2-digit" });

function ApprovalBar({ quote, total, marginPct, dirty, onDecided }) {
  const { t, locale } = useT();
  const { can } = useAuth();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const reasons = approvalReasons(quote.approval);
  const allowed = can("quotes.approve");
  const approvalId = quote.approval?.approval_id;
  const disabledReason = !allowed ? t("oq.appr.no_perm") : !approvalId ? t("oq.appr.no_id") : dirty ? t("oq.appr.save_first") : null;
  const decide = async (decision) => {
    if (decision === "rejected" && !note.trim()) { setError({ message: t("oq.appr.reject_note") }); return; }
    setBusy(decision); setError(null);
    try {
      await apiPost(`/approvals/${approvalId}/decide`, { decision, note: note.trim() || null });
      setNote("");
      onDecided(decision);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="mb-5 rounded-xl px-3 py-3 status-in-review" role="region" aria-label={t("oq.appr.title")}>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
        <div className="flex min-w-0 flex-col">
          <span className="text-sm font-medium">{t("oq.appr.title")}</span>
          <span className="text-sm">{reasons.length ? reasons.map((r) => (t(`oq.reason.${r}`) === `oq.reason.${r}` ? r : t(`oq.reason.${r}`))).join(" · ") : "—"}</span>
        </div>
        <div className="flex gap-5 text-sm">
          <span><span className="block text-xs opacity-80">{t("oq.q.total")}</span><span className="tabular font-medium">{eurTotal(total, locale)}</span></span>
          <span><span className="block text-xs opacity-80">{t("oq.q.margin")}</span><span className="tabular font-medium">{marginPct === null ? "—" : `${num(marginPct, locale, 1)} %`}</span></span>
        </div>
        <div className="flex min-w-[240px] flex-1 flex-wrap items-center justify-end gap-2">
          <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("oq.appr.note")} aria-label={t("oq.appr.note")}
            className="min-w-[180px] flex-1 bg-background sm:max-w-[320px]" disabled={!!disabledReason} />
          <Button size="sm" disabled={!!disabledReason || !!busy} onClick={() => decide("approved")}>{busy === "approved" ? t("state.saving") : t("action.approve")}</Button>
          <Button size="sm" variant="outline" className="bg-background" disabled={!!disabledReason || !!busy} onClick={() => decide("rejected")}>{t("action.reject")}</Button>
        </div>
      </div>
      {disabledReason && <p className="mt-2 text-xs">{disabledReason}</p>}
      {error && <ErrorState className="mt-2" compact error={error} />}
    </div>
  );
}

function ManualOption({ value, onChange, carriers, dgRequired }) {
  const { t } = useT();
  const set = (p) => onChange({ ...value, ...p, dirty: true });
  const carrierOpts = [...(carriers || []).filter((c) => c.active !== false).map((c) => ({ value: c.id, label: c.name })), { value: "__other", label: t("oq.manual.other_carrier") }];
  return (
    <section className="rounded-xl border border-dashed border-[color:var(--border-default)] p-3" aria-label={t("oq.manual.title")}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-sm font-medium">{t("oq.manual.title")}</h3>
        <Button size="xs" variant="ghost" onClick={() => onChange(null)}><X />{t("oq.manual.discard")}</Button>
      </div>
      <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)_120px_minmax(0,1.5fr)_100px]">
        <Field label={t("field.carrier")}>
          <NativeSelect value={value.carrier_id || (value.carrier_name !== null ? "__other" : null)} placeholder={t("oq.choose")} aria-label={t("field.carrier")}
            onChange={(v) => set(v === "__other" ? { carrier_id: null, carrier_name: "" } : { carrier_id: v, carrier_name: null })} options={carrierOpts} />
        </Field>
        {value.carrier_name !== null && !value.carrier_id ? (
          <Field label={t("oq.manual.carrier_name")} htmlFor="mo-cn">
            <Input id="mo-cn" value={value.carrier_name || ""} onChange={(e) => set({ carrier_name: e.target.value })} />
          </Field>
        ) : <div className="hidden md:block" />}
        <Field label={t("field.mode")}>
          <NativeSelect value={value.transport_mode} aria-label={t("field.mode")} onChange={(v) => set({ transport_mode: v })} options={MODES.map((m) => ({ value: m, label: tEnum(t, "mode", m) }))} />
        </Field>
        <Field label={t("oq.manual.service_name")} htmlFor="mo-sn">
          <Input id="mo-sn" value={value.service_name} onChange={(e) => set({ service_name: e.target.value })} placeholder={t("oq.rate.service_name_ph")} />
        </Field>
        <Field label={t("oq.rates.col.transit")} htmlFor="mo-td">
          <NumInput id="mo-td" value={value.transit_days} onChange={(v) => set({ transit_days: v })} />
        </Field>
      </div>
      <LinesEditor draft={value} onDraft={(d) => onChange({ ...value, ...d, dirty: true })} dgRequired={dgRequired} />
    </section>
  );
}

function QuoteView({ quote, setQuote, reload, refreshing, updatedAt }) {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("quotes.write");
  const locked = LOCKED.includes(quote.status) || !canWrite;
  const carriers = useApi(canWrite ? "/carriers?active=true" : null, { enabled: canWrite });

  const [drafts, setDrafts] = useState(() => (quote.options || []).map(initDraft));
  const [manual, setManual] = useState(null);
  const [validUntil, setValidUntil] = useState((quote.valid_until || "").slice(0, 10));
  const [note, setNote] = useState("");
  const [customerMsg, setCustomerMsg] = useState(quote.customer_message || "");
  const [saving, setSaving] = useState(null);
  const [saveErr, setSaveErr] = useState(null);
  const [saved, setSaved] = useState(null); // {at, sent, status}
  const [confirmRequote, setConfirmRequote] = useState(false);
  const defaultOpen = () => new Set([(quote.options || []).find((o) => o.recommended)?.id, quote.accepted_option_id].filter(Boolean));
  const [expanded, setExpanded] = useState(defaultOpen);

  // Re-initialise the editor when the server copy changes (after save / approve / requote).
  const serverKey = `${quote.id}:${quote.revision ?? ""}:${quote.updated_at ?? ""}:${quote.status}`;
  useEffect(() => {
    setDrafts((quote.options || []).map(initDraft));
    setManual(null);
    setValidUntil((quote.valid_until || "").slice(0, 10));
    setCustomerMsg(quote.customer_message || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey]);

  const req = quote.request || {};
  const cargo = req.cargo || {};
  const dgRequired = !!cargo.dangerous_goods;
  const dirtyOptions = drafts.filter((d) => d.dirty);
  const validChanged = validUntil && validUntil !== (quote.valid_until || "").slice(0, 10);
  const msgChanged = (customerMsg || "") !== (quote.customer_message || "");
  const dirty = dirtyOptions.length > 0 || !!manual || validChanged || msgChanged || !!note.trim();

  const optionById = useMemo(() => Object.fromEntries((quote.options || []).map((o) => [o.id, o])), [quote.options]);
  const rec = (quote.options || []).find((o) => o.recommended) || (quote.options || [])[0];
  const recDraft = rec ? drafts.find((d) => d.id === rec.id) : null;
  const recTotals = recDraft ? optionTotals(recDraft.lines) : null;
  const dgMissingAny = dgRequired && drafts.some((d) => !hasDgLine(d.lines));
  const pendingApproval = quote.status === "pending_approval" || quote.approval?.status === "pending";

  const manualValid = !manual || ((manual.carrier_id || (manual.carrier_name || "").trim()) && manual.transport_mode && manual.lines.length > 0);

  const save = async (send) => {
    if (!manualValid) { setSaveErr({ message: t("oq.manual.incomplete") }); return; }
    setSaving(send ? "send" : "save"); setSaveErr(null);
    const body = {};
    if (dirtyOptions.length) {
      body.options = dirtyOptions.map((d) => ({ id: d.id, lines: d.lines.map(cleanLine), margin_pct: d.mode === "set" ? d.markup : null }));
    }
    if (validChanged) body.valid_until = validUntil;
    if (note.trim()) body.note = note.trim();
    if (msgChanged) body.customer_message = customerMsg;
    if (manual) {
      body.add_manual_option = {
        ...(manual.carrier_id ? { carrier_id: manual.carrier_id } : { carrier_name: manual.carrier_name.trim() }),
        mode: manual.transport_mode, service_name: manual.service_name || null, transit_days: manual.transit_days, lines: manual.lines.map(cleanLine),
      };
    }
    if (send) body.send = true;
    try {
      const r = await apiPatch(`/quotes/${quote.id}`, body);
      const nq = r.quote || r;
      setNote("");
      setSaved({ at: new Date(), sent: !!send, status: nq.status });
      setQuote(nq);
    } catch (e) {
      setSaveErr(e); // edits stay in place
    } finally {
      setSaving(null);
    }
  };

  const requote = async () => {
    setSaving("requote"); setSaveErr(null);
    try {
      const r = await apiPost(`/quotes/${quote.id}/requote`);
      setConfirmRequote(false);
      setSaved({ at: new Date(), requoted: true, status: (r.quote || r).status });
      setQuote(r.quote || r);
    } catch (e) {
      setConfirmRequote(false);
      setSaveErr(e);
    } finally {
      setSaving(null);
    }
  };

  const vu = quote.valid_until;
  const vuDays = daysUntil(vu);
  const notes = quote.notes || [];
  const sendBlocked = pendingApproval ? t("oq.save.send_blocked_approval") : null;

  return (
    <div className="pb-24">
      <PageHeader
        crumbs={[{ label: t("nav.quotes"), to: "/ops/quotes" }, { label: quote.number || t("oq.q.no_number") }]}
        title={quote.number || t("oq.q.no_number")}
        meta={
          <>
            <StatusBadge group="quote" value={quote.status} />
            {quote.source === "ai" && <AgentMark />}
            <span>{t(`oq.q.src.${quote.source}`)}</span>
            <span>{t("oq.q.created_rel", { when: relTime(quote.created_at, locale) })}</span>
            {quote.revision > 1 && <span>{t("oq.q.revision", { n: quote.revision })}</span>}
            <Freshness at={updatedAt} onRefresh={reload} refreshing={refreshing} />
          </>
        }
        actions={
          <>
            <Button variant="outline" size="sm" asChild><a href={fileUrl(`/quotes/${quote.id}/pdf?lang=fi`)} target="_blank" rel="noreferrer"><FileText />{t("review.pdf_fi")}</a></Button>
            <Button variant="ghost" size="sm" asChild><a href={fileUrl(`/quotes/${quote.id}/pdf?lang=en`)} target="_blank" rel="noreferrer">{t("review.pdf_en")}</a></Button>
            {canWrite && !LOCKED.includes(quote.status) && (
              <Button variant="ghost" size="sm" onClick={() => setConfirmRequote(true)}><RefreshCw />{t("oq.q.requote")}</Button>
            )}
          </>
        }
      />

      {pendingApproval && (
        <ApprovalBar quote={quote} total={recTotals?.total ?? rec?.total_cents} marginPct={recTotals?.marginPct ?? rec?.margin_pct ?? null} dirty={dirty}
          onDecided={(decision) => { setSaved({ at: new Date(), decided: decision }); reload(); }} />
      )}

      <div className="mb-5">
        <KV cols={3} items={[
          { label: t("field.customer"), value: quote.company_id ? <Link to={`/ops/customers/${quote.company_id}`} className="hover:underline">{quote.customer_name || quote.company_id}</Link> : (quote.customer_name || t("oq.q.anonymous")),
            hint: (
              <>
                {quote.requested_by?.name ? t("oq.q.requested_by", { name: quote.requested_by.name }) : null}
                {quote.company?.kyc_status && quote.company.kyc_status !== "approved" && <span className="ml-1.5 text-warning-fg">{t("oq.q.kyc_open")}</span>}
                {quote.company?.sanctions_status && !["clear", "not_screened"].includes(quote.company.sanctions_status) && <span className="ml-1.5 text-danger-fg">{t("oq.q.sanctions", { status: quote.company.sanctions_status })}</span>}
              </>
            ) },
          { label: t("oq.q.route"), value: (
            <span className="flex flex-col">
              <span className="font-mono text-[13px]">{locCode(req.origin) || "—"} → {locCode(req.destination) || "—"}</span>
              <span className="text-xs text-ink-tertiary">{locName(req.origin) || "—"} → {locName(req.destination) || "—"}{req.direction ? ` · ${t(`oq.dir.${req.direction}`)}` : ""}</span>
            </span>
          ) },
          { label: t("oq.q.cargo"), value: cargoText(cargo, t, locale) },
          { label: t("field.incoterm"), value: req.incoterm ? `${req.incoterm}${req.incoterm_place ? ` ${req.incoterm_place}` : ""}` : null },
          { label: t("oq.q.ready"), value: dateNum(req.ready_date, locale), hint: req.mode ? tEnum(t, "mode", req.mode) : t("oq.q.all_modes") },
          { label: t("oq.q.valid_until"), value: vu ? (
            <span className={cn(vuDays !== null && vuDays < 0 && !LOCKED.includes(quote.status) && "text-danger-fg")}>
              {dateNum(vu, locale)}{vuDays !== null && !LOCKED.includes(quote.status) ? ` · ${vuDays < 0 ? t("oq.q.lapsed") : t("oq.q.days_left", { n: vuDays })}` : ""}
            </span>
          ) : null, hint: quote.terms_version || null },
          { label: t("oq.q.dg"), value: dgRequired ? `${cargo.dg?.un_number || "UN?"}${cargo.dg?.class ? ` · ${t("oq.dg.class")} ${cargo.dg.class}` : ""}${cargo.dg?.packing_group ? ` · PG ${cargo.dg.packing_group}` : ""}` : t("oq.q.no_dg") },
          { label: t("oq.q.services"), value: [req.customs_clearance ? t("oq.q.customs_yes") : t("oq.q.customs_no"), req.insurance?.cover && req.insurance.cover !== "NONE" ? `${t("oq.new.insurance")} ${req.insurance.cover.replace("_", " ")}` : t("oq.new.no_insurance")].join(" · ") },
          quote.shipment_id ? { label: t("oq.q.shipment"), value: <Link to={`/ops/shipments/${quote.shipment_id}`} className="hover:underline">{quote.shipment_reference || quote.shipment_id}</Link> } : null,
        ]} />
        {req.notes && <p className="mt-3 text-sm text-ink-secondary"><span className="text-xs text-ink-tertiary">{t("oq.q.customer_notes")}: </span>{req.notes}</p>}
      </div>

      <SourcingStatus quote={quote} />
      {(quote.ai?.summary || quote.warnings?.length > 0 || dgMissingAny) && (
        <div className="mb-5 flex flex-col gap-2">
          {quote.ai?.summary && (
            <p className="flex items-start gap-2 text-sm"><AgentMark className="mt-0.5 shrink-0" /><span className="text-ink-secondary">{quote.ai.summary}</span></p>
          )}
          <Warnings items={quote.warnings} />
          {dgMissingAny && <p className="text-sm text-warning-fg">{t("oq.opt.dg_missing_summary")}</p>}
        </div>
      )}

      <Section
        title={`${t("oq.q.options")} · ${num((quote.options || []).length, locale)}`}
        actions={
          <>
            {(quote.options || []).length > 1 && (
              <Button size="xs" variant="ghost" onClick={() => setExpanded((cur) => (cur.size >= drafts.length ? new Set() : new Set(drafts.map((d) => d.id))))}>
                {expanded.size >= drafts.length ? t("oq.opt.collapse_all") : t("oq.opt.expand_all")}
              </Button>
            )}
            {!locked && !manual && (
              <Button size="sm" variant="outline" onClick={() => setManual({ carrier_id: null, carrier_name: null, transport_mode: req.mode || "sea", service_name: "", transit_days: null, lines: [], markup: null, mode: "kept", dirty: true })}>
                <Plus />{t("oq.manual.add")}
              </Button>
            )}
          </>
        }
      >
        <div className="flex flex-col gap-4">
          {(quote.options || []).length === 0 && !manual && (
            <p className="text-sm text-ink-tertiary">{t("oq.q.no_options")}</p>
          )}
          {drafts.length > 0 && (
            <div className="overflow-hidden rounded-xl border border-[color:var(--border-subtle)]">
              <div className="hidden items-center gap-x-3 border-b border-[color:var(--border-subtle)] px-3 py-1.5 text-xs text-ink-tertiary md:flex" aria-hidden>
                <span className="w-4" /><span className="w-5">#</span>
                <span className="flex-1 basis-[220px]">{t("field.carrier")} · {t("oq.rates.col.service")}</span>
                <span className="hidden w-28 lg:block" />
                <span className="w-20 text-right">{t("oq.rates.col.transit")}</span>
                <span className="hidden w-[92px] sm:block">{t("oq.rates.col.source")}</span>
                <span className="hidden w-24 text-right sm:block">{t("oq.opt.cost")}</span>
                <span className="w-24 text-right">{t("oq.opt.sell")}</span>
                <span className="w-28 text-right">{t("oq.q.margin")}</span>
              </div>
              {drafts.map((d, i) => optionById[d.id] && (
                <OptionEditor key={d.id} option={optionById[d.id]} draft={d} readOnly={locked} dgRequired={dgRequired} rank={i + 1}
                  accepted={quote.accepted_option_id === d.id} expanded={expanded.has(d.id)}
                  onToggle={() => setExpanded((cur) => { const n = new Set(cur); if (n.has(d.id)) n.delete(d.id); else n.add(d.id); return n; })}
                  onDraft={(nd) => { setDrafts((all) => all.map((x) => (x.id === d.id ? nd : x))); setSaved(null); }} />
              ))}
            </div>
          )}
          {manual && <ManualOption value={manual} onChange={(m) => { setManual(m); setSaved(null); }} carriers={carriers.data?.items} dgRequired={dgRequired} />}
        </div>
      </Section>

      {quote.not_quoted?.length > 0 && (
        <Section title={t("oq.q.not_quoted")}>
          <ul className="flex flex-col gap-1 text-sm">
            {quote.not_quoted.map((n, i) => (
              <li key={i} className="flex flex-wrap gap-x-2">
                <span className="font-medium">{n.carrier_name || "—"}</span>
                <span className="text-ink-tertiary">{n.mode ? tEnum(t, "mode", n.mode) : ""}{n.service_name ? ` · ${n.service_name}` : ""}</span>
                <span className="text-ink-secondary">{n.reason}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <EmailPanel kind="quote" />
      <Section title={t("oq.q.notes_title")}>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="flex flex-col gap-2">
            {notes.length === 0 ? <p className="text-sm text-ink-tertiary">{t("oq.q.no_notes")}</p> : (
              <ul className="flex flex-col gap-2">
                {notes.map((n, i) => (
                  <li key={i} className="text-sm">
                    <span className="text-xs text-ink-tertiary">{n.author?.name || n.by?.name || "—"} · {dateTime(n.at || n.created_at, locale)}</span>
                    <p className="whitespace-pre-wrap">{n.text || n.note}</p>
                  </li>
                ))}
              </ul>
            )}
            {(quote.revisions || []).length > 0 && (
              <details className="text-sm">
                <summary className="cursor-pointer text-xs text-ink-secondary">{t("oq.q.history", { n: quote.revisions.length })}</summary>
                <ul className="mt-1 flex flex-col gap-1">
                  {[...quote.revisions].reverse().map((r, i) => (
                    <li key={i} className="text-xs text-ink-secondary">
                      <span className="text-ink-tertiary">{t("oq.q.revision", { n: r.revision })} · {r.edited_by?.name || "—"} · {dateTime(r.at, locale)}</span>
                      {(r.changes || []).length > 0 && <span> · {r.changes.map((c) => (typeof c === "string" ? c : c.summary || c.field || c.code || JSON.stringify(c))).join(", ")}</span>}
                      {r.valid_until && <span> · {t("oq.q.valid_short", { date: dateNum(r.valid_until, locale) })}</span>}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {!locked && (
              <Field label={t("oq.save.note")} htmlFor="qd-note" hint={t("oq.save.note_hint")}>
                <Textarea id="qd-note" value={note} onChange={(e) => setNote(e.target.value)} className="min-h-[56px]" />
              </Field>
            )}
          </div>
          <Field label={t("oq.save.customer_message")} htmlFor="qd-cm" hint={t("oq.save.customer_message_hint")}>
            <Textarea id="qd-cm" value={customerMsg} disabled={locked} onChange={(e) => setCustomerMsg(e.target.value)} className="min-h-[88px]" />
          </Field>
        </div>
      </Section>

      {!locked && (
        <div className="sticky bottom-0 z-10 -mx-4 border-t border-[color:var(--border-subtle)] bg-background/95 px-4 py-2 backdrop-blur supports-[backdrop-filter]:bg-background/80">
          {saveErr && <ErrorState className="mb-2" compact error={saveErr} />}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <label className="inline-flex items-center gap-2 text-sm">
              <span className="text-ink-secondary">{t("oq.q.valid_until")}</span>
              <DateInput value={validUntil} min={new Date().toISOString().slice(0, 10)} onChange={(iso) => { setValidUntil(iso); setSaved(null); }} className="w-40" aria-label={t("oq.q.valid_until")} />
            </label>
            <span className="text-xs text-ink-tertiary" aria-live="polite">
              {saving ? t("state.saving")
                : saved ? (saved.decided ? t(`oq.save.decided_${saved.decided}`) : saved.requoted ? t("oq.save.requoted", { time: hhmm(saved.at, locale) }) : saved.sent ? (saved.status === "pending_approval" ? t("oq.save.sent_needs_approval", { time: hhmm(saved.at, locale) }) : t("oq.save.sent_at", { time: hhmm(saved.at, locale) })) : t("oq.saved_at", { time: hhmm(saved.at, locale) }))
                : dirty ? t("oq.save.unsaved") : ""}
            </span>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              {dgMissingAny && <span className="text-xs text-warning-fg">{t("oq.save.dg_warn")}</span>}
              <Button variant="outline" disabled={!!saving || !dirty} onClick={() => save(false)}>{t("action.save")}</Button>
              <Button disabled={!!saving || !!sendBlocked} title={sendBlocked || undefined} onClick={() => save(true)}>{t("oq.save.save_send")}</Button>
            </div>
          </div>
          {sendBlocked && <p className="mt-1 text-right text-xs text-ink-tertiary">{sendBlocked}</p>}
        </div>
      )}
      {locked && canWrite && <p className="text-xs text-ink-tertiary">{t("oq.save.locked", { status: tEnum(t, "quote", quote.status) })}</p>}

      <AlertDialog open={confirmRequote} onOpenChange={setConfirmRequote}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("oq.q.requote_q")}</AlertDialogTitle>
            <AlertDialogDescription>{t("oq.q.requote_body")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("action.cancel")}</AlertDialogCancel>
            <AlertDialogAction disabled={saving === "requote"} onClick={(e) => { e.preventDefault(); requote(); }}>{t("oq.q.requote")}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

export default function QuoteDetail() {
  const { id } = useParams();
  const q = useApi(`/quotes/${id}`);
  return (
    <Loadable q={q} skeleton={<div><LoadingRows rows={3} /><LoadingRows rows={8} /></div>}>
      {(d) => (
        <QuoteView key={id} quote={d.quote || d} reload={q.reload} refreshing={q.refreshing} updatedAt={q.updatedAt}
          setQuote={(nq) => q.setData({ quote: nq })} />
      )}
    </Loadable>
  );
}

