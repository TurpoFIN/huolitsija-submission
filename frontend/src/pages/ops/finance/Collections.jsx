// Ops › Finance › Collections (Perintä). Decision: "what is the next step for each overdue customer case: final demand,
// payment plan, collection agency, write-off, or close as paid?" Rows show the invoice, customer, stage, what is owed
// (principal + late interest + recovery fee, EUR) and the next action date; opening a row shows its history and actions.
// APIs: GET /finance/collections?stage= {items, total, counts, totals, agency_mode},
// POST /finance/collections/{id}/{send_demand|payment_plan|agency|write_off|close} {note, installments[]},
// GET /finance/collections/{id}/file (agency hand-over file).
import React, { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ChevronDown, ChevronRight, Plus, ShieldAlert, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DateInput } from "@/components/ui/DateInput";
import { Segmented } from "@/components/app/FilterBar";
import { KV } from "@/components/app/layout";
import { AgentMark } from "@/components/app/StatusBadge";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { isoDate } from "@/features/billing/invoiceFormat";
import { ActionNote, ConfirmAction, Dot, FileLink, FinBar, FinStatus } from "@/features/finance/kit";
import { amount, eur, shiftPeriod, todayHelsinki } from "@/features/finance/format";

const STAGES = ["final_demand", "payment_plan", "agency", "legal", "paid", "written_off"];
const OPEN_STAGES = ["final_demand", "payment_plan", "agency", "legal"];
const NEEDS_NOTE = new Set(["write_off"]);
const DESTRUCTIVE = new Set(["write_off"]);

/** Actions the backend accepts for a case in this stage (routes/finance.py + services/collections.py). */
export function caseActions(c) {
  if (!OPEN_STAGES.includes(c.stage)) return [];
  const out = [];
  if (c.stage === "final_demand" && !c.demand) out.push("send_demand");
  if (["final_demand", "payment_plan", "agency"].includes(c.stage)) out.push("payment_plan");
  if (["final_demand", "payment_plan"].includes(c.stage)) out.push("agency");
  out.push("close", "write_off");
  return out;
}

const day = (iso) => (iso ? String(iso).slice(0, 10) : null);

function claimEur(c) {
  if (c.eur && c.eur.total_cents !== undefined) return c.eur.total_cents;
  if ((c.currency || "EUR") !== "EUR") return null;
  const parts = [c.amount_due_cents, c.interest_cents, c.fee_cents];
  return parts.some((x) => x === null || x === undefined) ? null : parts.reduce((a, b) => a + b, 0);
}

function PlanEditor({ c, rows, setRows }) {
  const { t } = useT();
  const cur = c.currency || "EUR";
  const set = (i, patch) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-ink-tertiary">{t("fin.col.plan_hint", { cur })}</p>
      {rows.map((r, i) => (
        <div key={i} className="flex items-center gap-2">
          <span className="w-5 text-xs text-ink-tertiary tabular">{i + 1}.</span>
          <span className="w-36"><DateInput value={r.due_date} onChange={(v) => set(i, { due_date: v })} min={todayHelsinki()} aria-label={t("fin.col.plan_date", { n: i + 1 })} /></span>
          <Input inputMode="decimal" className="w-32 text-right tabular" value={r.amount} onChange={(e) => set(i, { amount: e.target.value })} aria-label={t("fin.col.plan_amount", { n: i + 1 })} />
          <span className="text-xs text-ink-tertiary">{cur}</span>
          {rows.length > 1 && (
            <Button size="icon-sm" variant="ghost" onClick={() => setRows(rows.filter((_, j) => j !== i))} aria-label={t("action.delete")}><Trash2 /></Button>
          )}
        </div>
      ))}
      <Button size="xs" variant="ghost" className="self-start" onClick={() => setRows([...rows, { due_date: "", amount: "" }])}><Plus /> {t("fin.col.plan_add")}</Button>
    </div>
  );
}

const toCents = (s) => {
  const n = Number(String(s || "").replace(/\s/g, "").replace(",", "."));
  return Number.isFinite(n) ? Math.round(n * 100) : NaN;
};

function suggestPlan(c) {
  // Three monthly instalments of the principal + interest + fee in the case currency; the user edits before saving.
  const total = (c.amount_due_cents || 0) + (c.interest_cents || 0) + ((c.currency || "EUR") === "EUR" ? c.fee_cents || 0 : 0);
  const base = Math.floor(total / 3);
  const today = todayHelsinki();
  return [0, 1, 2].map((i) => {
    const p = shiftPeriod(today.slice(0, 7), i + 1);
    const cents = i === 2 ? total - base * 2 : base;
    return { due_date: `${p}-${today.slice(8, 10) > "28" ? "28" : today.slice(8, 10)}`, amount: (cents / 100).toFixed(2).replace(".", ",") };
  });
}

function CaseRow({ c, open, onToggle, canWrite, onAction }) {
  const { t, locale } = useT();
  const actions = caseActions(c);
  const cur = c.currency || "EUR";
  return (
    <li className={cn(open && "bg-[var(--bg-subtle)]")}>
      <button type="button" onClick={onToggle} aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 text-left hover:bg-hover">
        {open ? <ChevronDown className="size-4 shrink-0 text-ink-tertiary" aria-hidden /> : <ChevronRight className="size-4 shrink-0 text-ink-tertiary" aria-hidden />}
        <span className="w-24 shrink-0 font-mono text-[13px]">{c.invoice_number || "—"}</span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{c.company_name || "—"}</span>
        {c.credit_hold && <span className="inline-flex items-center gap-1 text-xs text-danger-fg"><ShieldAlert className="size-3.5" aria-hidden />{t("fin.col.credit_hold")}</span>}
        {c.pending_action && <span className="text-xs text-warning-fg">{t("fin.col.pending", { action: tEnum(t, "fin.col_action", c.pending_action.action) })}</span>}
        <FinStatus group="collections" value={c.stage} />
        <span className="w-28 text-right text-sm font-medium tabular">{eur(claimEur(c), locale)}</span>
        <span className="hidden w-32 text-right text-xs text-ink-tertiary tabular sm:inline">
          {OPEN_STAGES.includes(c.stage) && c.next_action_at ? t("fin.col.next_on", { date: isoDate(day(c.next_action_at), locale) }) : ""}
        </span>
      </button>
      {open && (
        <div className="grid gap-4 px-3 pb-4 pt-1 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <div className="min-w-0">
            <KV cols={2} items={[
              { label: t("fin.col.invoice"), value: c.invoice_id ? <Link className="text-ink-accent hover:underline" to={`/ops/billing/${c.invoice_id}`}>{c.invoice_number || t("action.open")}</Link> : c.invoice_number,
                hint: c.invoice_due_date ? t("fin.col.was_due", { date: isoDate(c.invoice_due_date, locale) }) : null },
              { label: t("fin.col.days_past_due"), value: c.days_past_due ?? null, className: "tabular" },
              { label: t("fin.col.principal"), value: amount(c.amount_due_cents, cur, locale), className: "tabular" },
              { label: t("fin.col.interest"), value: amount(c.interest_cents, cur, locale), className: "tabular",
                hint: c.late_interest_pct !== undefined && c.late_interest_pct !== null ? `${String(c.late_interest_pct).replace(".", locale === "fi" ? "," : ".")} %` : null },
              { label: t("fin.col.fee"), value: amount(c.fee_cents, c.fee_currency || "EUR", locale), className: "tabular", hint: c.fee_basis || null },
              { label: t("fin.col.claim_eur"), value: eur(claimEur(c), locale), className: "tabular font-medium" },
              { label: t("fin.col.opened"), value: c.opened_at ? isoDate(day(c.opened_at), locale) : null },
              { label: t("fin.col.next_action"), value: c.next_action_at && OPEN_STAGES.includes(c.stage) ? isoDate(day(c.next_action_at), locale) : null },
              c.lien_possible ? { label: t("fin.col.lien"), value: (c.lien_shipments || []).map((s) => s.reference).filter(Boolean).join(", ") || t("fin.col.lien_yes") } : null,
              c.company_id ? { label: t("field.customer"), value: <Link className="text-ink-accent hover:underline" to={`/ops/customers/${c.company_id}`}>{c.company_name || t("action.open")}</Link> } : null,
            ]} />
            {c.payment_plan?.installments?.length > 0 && (
              <div className="mt-3">
                <p className="mb-1 text-xs text-ink-tertiary">{t("fin.col.plan")}</p>
                <ul className="text-sm tabular">
                  {c.payment_plan.installments.map((i) => (
                    <li key={i.seq} className="flex justify-between gap-3"><span>{isoDate(i.due_date, locale)}</span><span>{amount(i.amount_cents, c.payment_plan.currency || cur, locale)}{i.paid ? ` · ${t("fin.col.paid")}` : ""}</span></li>
                  ))}
                </ul>
              </div>
            )}
            {c.pending_action?.approval_id && (
              <p className="mt-3 text-sm"><Link className="text-ink-accent hover:underline" to={`/ops/approvals?focus=${c.pending_action.approval_id}`}>{t("fin.col.open_approval")}</Link></p>
            )}
            {actions.length > 0 && (
              <div className="mt-4 flex flex-wrap gap-2">
                {actions.map((a) => (
                  <Button key={a} size="sm" variant={DESTRUCTIVE.has(a) ? "destructive" : "outline"} disabled={!canWrite || !!c.pending_action}
                    title={!canWrite ? t("fin.no_permission") : undefined} onClick={() => onAction(c, a)}>
                    {tEnum(t, "fin.col_action", a)}
                  </Button>
                ))}
                {c.handover?.file_id && <FileLink path={`/finance/collections/${c.id}/file`} label={t("fin.col.handover_file")} size="sm" />}
              </div>
            )}
          </div>
          <div className="min-w-0">
            <p className="mb-1 text-xs text-ink-tertiary">{t("fin.col.history")}</p>
            {(c.actions || []).length === 0 ? <p className="text-sm text-ink-tertiary">—</p> : (
              <ol className="flex flex-col gap-1.5">
                {[...c.actions].reverse().map((a, i) => (
                  <li key={i} className="flex flex-wrap items-baseline gap-x-2 text-sm">
                    <span className="w-28 shrink-0 text-xs text-ink-tertiary tabular">{dateTime(a.at, locale)}</span>
                    <span className="font-medium">{tEnum(t, "fin.col_action", a.action)}</span>
                    {a.by?.type === "agent" ? <AgentMark /> : a.by?.name ? <span className="text-xs text-ink-tertiary">{a.by.name}</span> : null}
                    {a.note && <span className="basis-full text-xs text-ink-secondary sm:pl-[7.5rem]">{a.note}</span>}
                  </li>
                ))}
              </ol>
            )}
          </div>
        </div>
      )}
    </li>
  );
}

export default function Collections() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("billing.write");
  const [params, setParams] = useSearchParams();
  const stage = params.get("stage") || "open";
  const q = useApi(`/finance/collections${STAGES.includes(stage) ? `?stage=${stage}` : ""}`, { pollMs: 60000 });
  const [openId, setOpenId] = useState(params.get("focus"));
  const [pending, setPending] = useState(null); // {c, action}
  const [plan, setPlan] = useState([]);
  const [note, setNote] = useState(null);
  useEffect(() => {
    if (pending?.action === "payment_plan") setPlan(suggestPlan(pending.c));
  }, [pending]);
  const counts = q.data?.counts || null;
  const totals = q.data?.totals || null;
  const openCount = counts ? OPEN_STAGES.reduce((a, s) => a + (counts[s] || 0), 0) : undefined;
  const options = useMemo(() => [
    { value: "open", label: t("fin.col.open"), count: openCount },
    ...STAGES.map((s) => ({ value: s, label: tEnum(t, "fin.collections", s), count: counts ? counts[s] || 0 : undefined })),
    { value: "all", label: t("fin.all") },
  ], [counts, openCount, t]);
  const setStage = (v) => {
    const next = new URLSearchParams(params);
    if (v !== "open") next.set("stage", v); else next.delete("stage");
    setParams(next, { replace: true });
  };
  const run = async (reason) => {
    const { c, action } = pending;
    const body = {};
    if (reason) body.note = reason;
    if (action === "payment_plan") {
      body.installments = plan.map((r) => ({ due_date: r.due_date, amount_cents: toCents(r.amount) }));
    }
    const r = await apiPost(`/finance/collections/${c.id}/${action}`, body);
    const waiting = r?.case?.pending_action || r?.result?.approval_id;
    setNote({ text: waiting ? t("fin.col.done_pending", { action: tEnum(t, "fin.col_action", action), n: c.invoice_number || "" })
      : t("fin.col.done", { action: tEnum(t, "fin.col_action", action), n: c.invoice_number || "" }) });
    if (r?.case) q.setData((d) => ({ ...d, items: d.items.map((x) => (x.id === r.case.id ? r.case : x)) }));
    q.reload();
    return true;
  };

  const items = (q.data?.items || []).filter((c) => (stage === "open" ? OPEN_STAGES.includes(c.stage) : true));
  const planBad = pending?.action === "payment_plan" && (plan.length === 0 || plan.some((r) => !r.due_date || !(toCents(r.amount) > 0)));
  return (
    <div className="max-w-[1152px]">
      <FinBar title={t("fin.tab.collections")}
        meta={<>
          {totals && <span className={totals.open_count ? "text-foreground" : undefined}>{t("fin.ov.cases_n", { n: totals.open_count || 0 })}</span>}
          {totals?.open_claim_eur_cents ? <span className="tabular text-foreground">{eur(totals.open_claim_eur_cents, locale)}</span> : null}
          {totals?.pending_count > 0 && <><Dot /><span className="text-warning-fg">{t("fin.col.pending_n", { n: totals.pending_count })}</span></>}
          {totals?.credit_hold_count > 0 && <><Dot /><span className="text-danger-fg">{t("fin.ov.holds_n", { n: totals.credit_hold_count })}</span></>}
          {q.data?.agency_mode && <><Dot /><span className="text-xs text-ink-tertiary">{t("fin.col.agency_mode", { mode: q.data.agency_mode })}</span></>}
          <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
        </>} />
      <div className="mb-3"><Segmented label={t("fin.col.stage")} value={stage} onChange={setStage} options={options} /></div>
      <ActionNote note={note} onClose={() => setNote(null)} className="mb-3" />
      <Loadable q={q} skeleton={<LoadingRows rows={6} />} isEmpty={() => items.length === 0}
        empty={<EmptyState title={stage === "open" ? t("fin.col.empty") : t("fin.none_match")} body={stage === "open" ? t("fin.col.empty_body") : null} />}>
        {() => (
          <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
            {items.map((c) => (
              <CaseRow key={c.id} c={c} open={openId === c.id} onToggle={() => setOpenId(openId === c.id ? null : c.id)}
                canWrite={canWrite} onAction={(cc, a) => setPending({ c: cc, action: a })} />
            ))}
          </ul>
        )}
      </Loadable>
      <ConfirmAction open={!!pending} onOpenChange={(o) => !o && setPending(null)}
        title={pending ? `${tEnum(t, "fin.col_action", pending.action)} · ${pending.c.invoice_number || ""}` : ""}
        description={pending ? t(`fin.col.confirm.${pending.action}`) : null}
        submitLabel={pending ? tEnum(t, "fin.col_action", pending.action) : ""}
        reason={pending ? pending.action !== "payment_plan" : false}
        reasonRequired={pending ? NEEDS_NOTE.has(pending.action) : false} destructive={pending ? DESTRUCTIVE.has(pending.action) : false}
        onSubmit={(r) => (planBad ? false : run(r))}>
        {pending?.action === "payment_plan" && <PlanEditor c={pending.c} rows={plan} setRows={setPlan} />}
      </ConfirmAction>
    </div>
  );
}
