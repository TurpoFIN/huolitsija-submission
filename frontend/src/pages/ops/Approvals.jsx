import React, { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Clock } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { PageHeader, KV } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { StatusBadge, AgentMark } from "@/components/app/StatusBadge";
import { EmptyState, Loadable } from "@/components/app/states";
import { FourEyesNotice } from "@/components/app/FourEyes";
import { apiPost, useApi } from "@/lib/api";
import { dateTime, money, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { fourEyesState } from "@/lib/fourEyes";
import { toast } from "sonner";
import { agentName, legalBasis, pick, slaChip } from "@/features/autopilot/shared";

const KINDS = ["quote_threshold", "customs_filing", "claim", "ai_message", "kyc_review", "credit_limit",
  "sanctions_review", "dg_review", "privacy_erasure",  // s12 compliance
  "prospect_email"];  // s7 CRM: first e-mail to a prospect while Autopilot is off

/** Owner-queue deadline: time left, or how late, plus how many reminders went out. Neutral while there is time. */
function SlaChip({ a }) {
  const { t } = useT();
  if (a.status !== "pending") return null;
  const chip = slaChip(t, a);
  if (!chip) return null;
  const label = a.escalation_level > 0 ? `${chip.label} · ${t("ap.sla.reminded", { n: a.escalation_level })}` : chip.label;
  if (chip.state) return <StatusBadge size="sm" state={chip.state} label={label} />;
  return <span className="inline-flex h-5 items-center gap-1 whitespace-nowrap rounded-full bg-secondary px-2 text-[11px] font-medium text-ink-secondary"><Clock className="size-3" aria-hidden />{label}</span>;
}

/** Legally required human decision: the rule, why a person decides, and its basis. */
function LegalNote({ a }) {
  const { t, locale } = useT();
  if (!a.legal_point) return null;
  return (
    <div className="mt-3 rounded-lg border border-[color:var(--border-subtle)] p-3 text-sm">
      <p className="flex flex-wrap items-center gap-2"><StatusBadge size="sm" state="flagged" label={t("ap.legal.badge")} /><span className="font-medium">{pick(locale, a.legal_title_fi, a.legal_title_en)}</span></p>
      <p className="mt-1 text-ink-secondary">{pick(locale, a.legal_reason_fi, a.legal_reason_en)}</p>
      {a.legal_basis && <p className="mt-1 text-xs text-ink-tertiary">{t("ap.legal.basis")}: {legalBasis(locale, a.legal_basis)}</p>}
    </div>
  );
}

function Decision({ a, onDone }) {
  const { t } = useT();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [fourEyes, setFourEyes] = useState(a.four_eyes || null);
  const decide = async (decision, override = false) => {
    setBusy(true);
    try {
      await apiPost(`/approvals/${a.id}/decide`, { decision, note: note || null, ...(override ? { override: true } : {}) });
      toast.success(decision === "approved" ? t("action.approve") : t("action.reject"));
      onDone();
    } catch (e) {
      const state = fourEyesState(e);
      if (state) setFourEyes(state); else toast.error(e.message);
    } finally { setBusy(false); }
  };
  if (!a.can_decide) return <p className="text-xs text-ink-tertiary">{t("approvals.no_permission")}</p>;
  return (
    <div className="flex flex-col gap-2">
      <FourEyesNotice state={fourEyes} note={note} busy={busy} onOverride={() => decide("approved", true)} />
      <Textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("approvals.note")} className="min-h-[56px]" aria-label={t("approvals.note")} />
      <div className="flex gap-2">
        {!fourEyes && <Button size="sm" disabled={busy} onClick={() => decide("approved")}>{t("action.approve")}</Button>}
        <Button size="sm" variant="destructive" disabled={busy} onClick={() => decide("rejected")}>{t("action.reject")}</Button>
      </div>
    </div>
  );
}

function kindLabel(t, kind) {
  const key = `approvals.kind.${kind}`;
  const s = t(key);
  return s === key ? kind : s;
}

function bodyText(a) {
  const d = a.data || {};
  return d.message_body || d.body || d.draft || null;
}

export default function Approvals() {
  const { t, locale } = useT();
  const [params] = useSearchParams();
  const [status, setStatus] = useState("pending");
  const [kind, setKind] = useState(null);
  const focus = params.get("focus");
  const [openId, setOpenId] = useState(focus);
  const focusDone = useRef(null);
  const path = `/approvals?status=${status}${kind ? `&kind=${kind}` : ""}`;
  const q = useApi(path, { pollMs: 30000 });
  const kindOptions = useMemo(() => {
    const c = q.data?.pending_counts || {};
    const extra = Object.keys(c).filter((k) => !KINDS.includes(k) && (c[k] || k === kind));
    if (kind && !KINDS.includes(kind) && !extra.includes(kind)) extra.push(kind);
    return [{ value: null, label: t("approvals.all"), count: Object.values(c).reduce((a, b) => a + b, 0) },
      ...[...KINDS, ...extra].map((k) => ({ value: k, label: kindLabel(t, k), count: c[k] || 0 }))];
  }, [q.data, t, kind]);
  // ?focus=<id> (owner e-mails, the bell, Today, the decision log) opens that card and scrolls to it. A card that is no
  // longer pending is looked up under "all".
  const loadedPath = useRef(null);
  useEffect(() => { if (q.data) loadedPath.current = path; }, [q.updatedAt]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (focus) setOpenId(focus); }, [focus]);
  useEffect(() => {
    if (!focus || !q.data || focusDone.current === focus || loadedPath.current !== path) return;
    if (!q.data.items.some((a) => a.id === focus)) {
      if (status === "pending" && !kind) setStatus("all"); else focusDone.current = focus;
      return;
    }
    focusDone.current = focus;
    setTimeout(() => document.getElementById(`approval-${focus}`)?.scrollIntoView({ block: "center" }), 50);
  }, [focus, q.data, q.updatedAt, path, status, kind]);

  return (
    <div className="max-w-[1152px]">
      <PageHeader title={t("approvals.title")} />
      <div className="mb-3 flex flex-wrap gap-2">
        <Segmented value={status} onChange={setStatus} label={t("field.status")} options={[{ value: "pending", label: t("approvals.status.pending") }, { value: "all", label: t("approvals.status.all") }]} />
        <Segmented value={kind} onChange={setKind} label={t("review.kind")} options={kindOptions} />
      </div>
      <Loadable q={q} isEmpty={(d) => d.items.length === 0} empty={<EmptyState title={t("approvals.empty")} />}>
        {(d) => (
          <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
            {d.items.map((a) => {
              const open = openId === a.id;
              const text = bodyText(a);
              const fi = locale === "fi";
              const title = fi ? a.title_fi || a.title : a.title;
              const summary = fi ? a.summary_fi || a.summary : a.summary;
              const reason = fi ? a.reason_fi || a.reason : a.reason;
              const fromAgent = a.agent || a.requested_by?.type === "agent";
              const agentCode = a.agent || a.requested_by?.agent;
              const requester = fi ? a.requested_by?.name_fi || a.requested_by?.name : a.requested_by?.name;
              return (
                <li key={a.id} id={`approval-${a.id}`} className={cn(open && "bg-[var(--bg-subtle)]")}>
                  <button type="button" onClick={() => setOpenId(open ? null : a.id)} aria-expanded={open}
                    className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 text-left hover:bg-hover">
                    <span className="w-full shrink-0 text-sm font-medium sm:w-44">{kindLabel(t, a.kind)}</span>
                    <span className="min-w-[14rem] flex-1 whitespace-normal break-words text-sm">{title}</span>
                    {a.legal_point && <StatusBadge size="sm" state="flagged" label={t("ap.legal.badge")} />}
                    {fromAgent && <AgentMark label={agentCode ? agentName(t, locale, agentCode) : undefined} title={t("ap.agent_mark")} />}
                    <SlaChip a={a} />
                    {a.amount_cents ? <span className="tabular text-sm">{money(a.amount_cents, locale)}</span> : null}
                    <StatusBadge size="sm" group="approval" value={a.status} />
                    <span className="w-24 text-right text-xs text-ink-tertiary">{relTime(a.created_at, locale)}</span>
                  </button>
                  {open && (
                    <div className="grid gap-4 px-3 pb-4 pt-1 md:grid-cols-[minmax(0,1fr)_320px]">
                      <div className="min-w-0">
                        <KV cols={2} items={[
                          { label: t("approvals.reason"), value: a.reasons?.length > 1 ? (
                            <ul className="flex flex-col gap-1">{a.reasons.map((r, i) => <li key={i}>{pick(locale, r.fi, r.en)}</li>)}</ul>
                          ) : reason },
                          { label: t("approvals.requested_by"), value: requester },
                          a.sla_due_at && { label: t("ap.sla.due"), value: dateTime(a.sla_due_at, locale) },
                          { label: t("field.created"), value: dateTime(a.created_at, locale) },
                          a.decided_at && { label: t("approvals.decided"), value: `${dateTime(a.decided_at, locale)} · ${a.decided_by?.name || ""}${a.decision_note ? ` · ${a.decision_note}` : ""}` },
                        ]} />
                        {summary && <p className="mt-3 text-sm text-ink-secondary whitespace-pre-wrap">{summary}</p>}
                        <LegalNote a={a} />
                        {text && <div className="mt-3 rounded-lg bg-surface p-3 text-sm whitespace-pre-wrap shadow-[inset_0_0_0_0.5px_var(--border-default)]">{text}</div>}
                        <div className="mt-2 flex flex-wrap gap-x-4">
                          {a.link && <Button variant="link" size="sm" asChild><Link to={a.link}>{t("approvals.open_record")}</Link></Button>}
                          {a.agent_decision_id && <Button variant="link" size="sm" asChild><Link to="/ops/autopilot?tab=decisions">{t("ap.open_decision_log")}</Link></Button>}
                        </div>
                      </div>
                      {a.status === "pending" && <Decision a={a} onDone={q.reload} />}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Loadable>
    </div>
  );
}
