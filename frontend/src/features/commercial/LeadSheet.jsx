// One lead: why it scored, who we may write to and on what lawful basis, what the agent already sent, what happens next.
import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { Ban, Building2, Send } from "lucide-react";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { KV, Section } from "@/components/app/layout";
import { Timeline } from "@/components/app/Timeline";
import { useConfirmChange } from "@/components/app/ConfirmChange";
import { apiPatch, apiPost, useApi } from "@/lib/api";
import { date, dateTime, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { NativeSelect } from "@/features/quotes/ops/common";
import { ComBadge, ComLoadable, Eur, LeadState, Reasons, Score, SourceLink, mutationError, useLabel } from "./common";
import { STAGE_ORDER, activity, contact, lead as normLead, listOf } from "./shapes";

const MOVABLE = STAGE_ORDER.filter((s) => !["disqualified", "opted_out"].includes(s));
const TONE = { opt_out: "warning", opted_out: "warning", bounce: "danger", bounced: "danger", failed: "danger" };

export function LeadSheet({ id, open, onOpenChange, onChanged, canWrite }) {
  const { t, locale } = useT();
  const q = useApi(open && id ? `/crm/leads/${id}` : null);
  const acts = useApi(open && id ? `/crm/activities?lead_id=${encodeURIComponent(id)}` : null);
  const l = q.data ? normLead(q.data.lead || q.data, locale) : null;
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-[620px]">
        <SheetHeader className="border-b border-[color:var(--border-subtle)] px-5 py-4 text-left">
          <SheetTitle className="text-md font-medium pr-8">{l?.name || t("com.lead.title")}</SheetTitle>
          <SheetDescription className="flex flex-wrap items-center gap-2 text-xs text-ink-tertiary">
            {l?.business_id && <span className="font-mono">{l.business_id}</span>}
            {l?.city && <span>{l.city}</span>}
            {l && <LeadState lead={l} />}
          </SheetDescription>
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <ComLoadable q={q} perm="customers.write">
            {(d) => <Body l={normLead(d.lead || d, locale)} raw={d} acts={acts} canWrite={canWrite}
              onChanged={() => { q.reload(); acts.reload(); onChanged?.(); }} />}
          </ComLoadable>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function Body({ l, raw, acts, canWrite, onChanged }) {
  const { t, locale } = useT();
  const label = useLabel();
  const navigate = useNavigate();
  const { confirm, dialog } = useConfirmChange();
  const [busy, setBusy] = useState(null);
  const [stage, setStage] = useState(l.stage);
  const [why, setWhy] = useState("");
  const [dq, setDq] = useState(false);
  const contacts = listOf(raw.contacts || l.contacts).map((c) => contact(c, locale));
  const events = listOf(acts.data, "activities").map((a) => activity(a, locale))
    .sort((a, b) => String(a.at || "").localeCompare(String(b.at || "")))
    .map((a) => ({ id: a.id, at: a.at, title: `${label("act", a.kind)}${a.subject ? ` · ${a.subject}` : ""}`,
      detail: [a.summary, a.intent ? label("intent", a.intent) : null].filter(Boolean).join(" · ") || null,
      source: a.actor === "agent" ? t("com.lead.by_agent") : a.actor, tone: TONE[a.kind] }));
  if (l.next_follow_up_at) events.push({ id: "next", at: l.next_follow_up_at, title: t("com.lead.next_follow_up"), future: true });

  const act = async (key, fn, ok) => {
    setBusy(key);
    try {
      const r = await fn();
      toast.success(ok);
      onChanged?.(r);
      return r;
    } catch (e) {
      toast.error(mutationError(e, t, locale));
      return null;
    } finally {
      setBusy(null);
    }
  };
  const contactNow = () => act("contact", () => apiPost(`/crm/leads/${l.id}/contact`, {}), t("com.lead.contacted"));
  const move = () => confirm(() => act("stage", () => apiPatch(`/crm/leads/${l.id}`, { stage }), t("com.lead.moved", { stage: label("stage", stage) })),
    t("com.lead.move_q", { stage: label("stage", stage) }));
  const disqualify = () => act("dq", () => apiPatch(`/crm/leads/${l.id}`, { stage: "disqualified", disqualify_reason: why || null }), t("com.lead.disqualified")).then(() => setDq(false));
  const convert = () => confirm(async () => {
    const r = await act("convert", () => apiPost(`/crm/leads/${l.id}/convert`, {}), t("com.lead.converted"));
    const cid = r?.company_id || r?.company?.id || r?.customer_id;
    if (cid) navigate(`/ops/customers/${cid}`);
  }, t("com.lead.convert_q", { name: l.name }));

  const closed = ["won", "lost", "disqualified", "opted_out"].includes(l.stage);
  const canSend = !closed && !l.opted_out && contacts.some((c) => !c.opted_out);
  return (
    <div>
      {canWrite && (
        <div className="mb-5 flex flex-wrap items-center gap-2">
          <Button size="sm" onClick={contactNow} disabled={!canSend || !!busy}><Send />{busy === "contact" ? t("state.saving") : t("com.lead.contact_now")}</Button>
          {l.customer_id ? (
            <Button size="sm" variant="outline" onClick={() => navigate(`/ops/customers/${l.customer_id}`)}><Building2 />{t("com.lead.open_customer")}</Button>
          ) : (
            <Button size="sm" variant="outline" onClick={convert} disabled={!!busy || l.stage === "disqualified"}><Building2 />{t("com.lead.convert")}</Button>
          )}
          {!closed && <Button size="sm" variant="destructive" onClick={() => setDq((v) => !v)} aria-expanded={dq}><Ban />{t("com.lead.disqualify")}</Button>}
          {!canSend && !closed && <span className="text-xs text-ink-tertiary">{l.opted_out ? t("com.lead.opted_out") : t("com.lead.no_contact")}</span>}
        </div>
      )}
      {dq && (
        <div className="mb-5 flex flex-col gap-2 rounded-lg p-3 shadow-[inset_0_0_0_0.5px_var(--border-default)]">
          <label htmlFor="dq-why" className="text-xs font-medium text-ink-secondary">{t("com.lead.dq_reason")}</label>
          <Textarea id="dq-why" value={why} onChange={(e) => setWhy(e.target.value)} className="min-h-[56px]" />
          <div className="flex gap-2">
            <Button size="sm" variant="destructive" onClick={disqualify} disabled={!!busy}>{t("com.lead.disqualify")}</Button>
            <Button size="sm" variant="ghost" onClick={() => setDq(false)}>{t("action.cancel")}</Button>
          </div>
        </div>
      )}

      <KV cols={2} className="mb-5" items={[
        { label: t("com.lead.score"), value: <Score value={l.score} /> },
        { label: t("com.lead.value"), value: l.value_cents !== null ? <Eur cents={l.value_cents} /> : null, hint: l.value_cents !== null ? t("com.per_year") : null },
        { label: t("com.lead.industry"), value: [l.industry_code, l.industry].filter(Boolean).join(" · ") || null },
        { label: t("com.lead.next_follow_up"), value: l.next_follow_up_at ? `${date(l.next_follow_up_at, locale)} (${relTime(l.next_follow_up_at, locale)})` : null },
        { label: t("com.lead.last_contacted"), value: l.last_contacted_at ? dateTime(l.last_contacted_at, locale) : null },
        { label: t("com.lead.source"), value: (l.source_url || l.source_kind) ? (
          <span className="flex flex-wrap gap-x-3">
            <SourceLink href={l.source_url}>{label("src", l.source_kind || "prh")}</SourceLink>
            {l.trade_url && <SourceLink href={l.trade_url}>{t("com.src.eurostat")}</SourceLink>}
          </span>) : null },
      ]} />

      {canWrite && !["disqualified", "opted_out"].includes(l.stage) && (
        <div className="mb-5 flex flex-wrap items-end gap-2">
          <div className="flex min-w-[180px] flex-1 flex-col gap-1">
            <label htmlFor="lead-stage" className="text-xs font-medium text-ink-secondary">{t("com.lead.move_stage")}</label>
            <NativeSelect id="lead-stage" value={stage} onChange={(v) => setStage(v || l.stage)}
              options={MOVABLE.map((s) => ({ value: s, label: label("stage", s) }))} />
          </div>
          <Button size="sm" variant="outline" onClick={move} disabled={stage === l.stage || !!busy}>{t("com.lead.move")}</Button>
        </div>
      )}

      <Section title={t("com.lead.why_score")}>
        <Reasons items={l.score_reasons} />
      </Section>

      <Section title={t("com.lead.contacts")}>
        {contacts.length === 0 ? <p className="text-sm text-ink-tertiary">{t("com.lead.no_contacts")}</p> : (
          <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)]">
            {contacts.map((c, i) => (
              <li key={c.id || c.email || i} className="flex flex-col gap-0.5 py-2">
                <span className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-mono text-[13px] break-all">{c.email || "—"}</span>
                  {c.role && <span className="text-xs text-ink-tertiary">{label("role", c.role)}</span>}
                  {c.opted_out && <ComBadge group="stage" value="opted_out" />}
                </span>
                <span className="text-xs text-ink-tertiary">
                  {t("com.lead.lawful_basis")}: {c.lawful_basis ? label("basis", c.lawful_basis) : "—"}
                  {c.source_url && <> · <SourceLink href={c.source_url}>{t("com.lead.found_at")}</SourceLink></>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title={t("com.lead.activity")}>
        <ComLoadable q={acts} isEmpty={() => events.length === 0} empty={<p className="text-sm text-ink-tertiary">{t("com.lead.no_activity")}</p>}>
          {() => <Timeline events={events} />}
        </ComLoadable>
      </Section>
      {dialog}
    </div>
  );
}
