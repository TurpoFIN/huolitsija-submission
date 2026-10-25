import React, { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { KV, Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { dateTime, money, num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Readable } from "./Readable";
import { OUTCOME_STATE, agentName, legalBasis, pick } from "./shared";

const OUTCOMES = ["auto", "owner_queue", "blocked"];

/** Decision title in the UI language; old autonomy actions carry only their kind. An action without any readable
 *  title is shown as its code (mono), never dressed up as prose. */
export function decisionTitle(t, locale, d) {
  const title = pick(locale, d.title_fi, d.title);
  if (title && title !== d.action) return title;
  const key = `domain.action.${d.action}`;
  const s = t(key);
  if (s !== key) return s;
  return <span className="font-mono text-xs">{d.action}</span>;
}

const SUBJECT_LINK = {
  shipment: (id) => `/ops/shipments/${id}`, quote: (id) => `/ops/quotes/${id}`, invoice: (id) => `/ops/billing/${id}`,
  customs_declaration: (id) => `/ops/customs/${id}`, company: (id) => `/ops/customers/${id}`, carrier: (id) => `/ops/carriers/${id}`,
  exception: (id) => `/ops/exceptions?focus=${id}`, claim: (id) => `/ops/claims?id=${id}`,
};

export function subjectText(t, d) {
  if (d.subject_label) return d.subject_label;
  if (!d.subject_type) return null;
  const key = `ap.subject.${d.subject_type}`;
  const s = t(key);
  return `${s === key ? d.subject_type : s} ${String(d.subject_id || "").slice(0, 8)}`;
}

function ReasonList({ reasons, legal }) {
  const { t, locale } = useT();
  if (!reasons?.length) return <p className="text-sm text-ink-tertiary">{t("ap.dec.no_reasons")}</p>;
  return (
    <ul className="flex flex-col gap-2">
      {reasons.map((r, i) => {
        const lp = r.legal_point && legal?.[r.legal_point];
        return (
          <li key={i} className="text-sm">
            {r.code === "legal" && <StatusBadge size="sm" state="flagged" label={t("ap.legal.badge")} className="mr-2 align-middle" />}
            <span>{pick(locale, r.fi, r.en)}</span>
            {lp && <span className="mt-0.5 block text-xs text-ink-tertiary">{t("ap.legal.basis")}: {legalBasis(locale, lp.basis)}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function Guardrails({ g }) {
  const { t, locale } = useT();
  if (!g) return null;
  const m = (c) => money(c, locale, { decimals: 2 });
  const lines = [];
  if (g.confidence_floor !== undefined) lines.push({ label: t("ap.dec.g.floor"), value: num(g.confidence_floor, locale, 2) });
  if (g.spend && Object.keys(g.spend).length) lines.push({ label: t("ap.dec.g.spend"), value: t("ap.dec.g.spend_value", { today: m(g.spend.today_cents), this: m(g.spend.this_cents), limit: m(g.spend.limit_cents) }) });
  if (g.credit && Object.keys(g.credit).length) lines.push({ label: t("ap.dec.g.credit"), value: t("ap.dec.g.credit_value", { used: m(g.credit.used_cents), this: m(g.credit.this_cents), limit: m(g.credit.limit_cents) }) });
  if (g.claim && Object.keys(g.claim).length) lines.push({ label: t("ap.dec.g.claim"), value: t("ap.dec.g.claim_value", { claim: m(g.claim.claim_cents), limit: m(g.claim.limit_cents), insurance: m(g.claim.insurance_limit_cents) }) });
  if (!lines.length) return null;
  return <KV cols={1} items={lines} />;
}

function DecisionSheet({ id, onClose, legal }) {
  return (
    <Sheet open={!!id} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
        {id && <DecisionBody key={id} id={id} legal={legal} />}
      </SheetContent>
    </Sheet>
  );
}

function DecisionBody({ id, legal }) {
  const { t, locale } = useT();
  const q = useApi(`/autopilot/decisions/${id}`);
  return (
    <Loadable q={q} skeleton={<><SheetTitle className="sr-only">{t("ap.dec.title")}</SheetTitle><SheetDescription className="sr-only">{t("state.loading")}</SheetDescription><LoadingRows rows={6} /></>}>
          {(d) => (
            <div className="flex flex-col gap-5">
              <div>
                <SheetTitle className="pr-6 text-md font-medium">{decisionTitle(t, locale, d)}</SheetTitle>
                <SheetDescription className="mt-1 flex flex-wrap items-center gap-2 text-sm text-ink-secondary">
                  <StatusBadge size="sm" state={OUTCOME_STATE[d.outcome] || "expired"} label={t(`ap.outcome.${d.outcome}`)} />
                  <span>{agentName(t, locale, d.agent, d.agent_label_fi, d.agent_label_en)} · {dateTime(d.at, locale)}</span>
                </SheetDescription>
              </div>
              <KV items={[
                { label: t("ap.dec.subject"), value: SUBJECT_LINK[d.subject_type] && d.subject_id
                  ? <Link className="text-ink-accent hover:underline" to={SUBJECT_LINK[d.subject_type](d.subject_id)}>{subjectText(t, d)}</Link> : subjectText(t, d) },
                { label: t("ap.dec.action"), value: d.action, mono: true },
                { label: t("ap.dec.confidence"), value: d.confidence === null || d.confidence === undefined ? null : num(d.confidence, locale, 2), hint: d.guardrails?.confidence_floor !== undefined ? t("ap.dec.floor_hint", { floor: num(d.guardrails.confidence_floor, locale, 2) }) : undefined },
                { label: t("ap.dec.model"), value: d.model, mono: true },
                { label: t("ap.dec.cost"), value: money(d.cost_cents, locale, { decimals: 2 }), className: "tabular" },
                { label: t("ap.dec.spend"), value: money(d.spend_cents, locale, { decimals: 2 }), className: "tabular" },
                { label: t("ap.dec.autopilot_then"), value: t(d.autopilot_enabled ? "ap.on" : "ap.off") },
                d.approval_id && { label: t("ap.dec.approval"), value: <Link className="text-ink-accent hover:underline" to={`/ops/approvals?focus=${d.approval_id}`}>{t("ap.dec.open_approval")}</Link> },
              ]} />
              <Section title={t("ap.dec.reasons")} className="mb-0"><ReasonList reasons={d.reasons} legal={legal} /></Section>
              {d.guardrails && Object.keys(d.guardrails).length > 0 && <Section title={t("ap.dec.guardrails")} className="mb-0"><Guardrails g={d.guardrails} /></Section>}
              <Section title={t("ap.dec.decision")} className="mb-0"><Readable value={d.decision} /></Section>
              <Section title={t("ap.dec.inputs")} className="mb-0"><Readable value={d.inputs} /></Section>
              <details className="text-sm">
                <summary className="cursor-pointer text-ink-secondary">{t("ap.dec.raw")}</summary>
                <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-secondary p-3 font-mono text-xs">{JSON.stringify(d, null, 2)}</pre>
              </details>
            </div>
          )}
    </Loadable>
  );
}

export function DecisionLog({ legal }) {
  const { t, locale } = useT();
  const [agent, setAgent] = useState(null);
  const [outcome, setOutcome] = useState(null);
  const [sel, setSel] = useState(null);
  const q = useApi(`/autopilot/decisions?limit=300${agent ? `&agent=${encodeURIComponent(agent)}` : ""}${outcome ? `&outcome=${outcome}` : ""}`, { pollMs: 30000 });
  const agents = q.data?.agents;
  const counts = useMemo(() => {
    const rows = (agents || []).filter((a) => !agent || a.agent === agent);
    const c = { auto: 0, owner_queue: 0, blocked: 0 };
    rows.forEach((a) => OUTCOMES.forEach((o) => { c[o] += a[o] || 0; }));
    return c;
  }, [agents, agent]);
  const total = counts.auto + counts.owner_queue + counts.blocked;
  const filtered = agent || outcome;
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented label={t("ap.dec.outcome")} value={outcome} onChange={setOutcome} options={[
          { value: null, label: t("ap.all"), count: agents ? total : undefined },
          ...OUTCOMES.map((o) => ({ value: o, label: t(`ap.outcome.${o}`), count: agents ? counts[o] : undefined })),
        ]} />
        <Select value={agent || "all"} onValueChange={(v) => setAgent(v === "all" ? null : v)}>
          <SelectTrigger className="w-full sm:w-64" aria-label={t("ap.dec.agent")}><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t("ap.dec.all_agents")}</SelectItem>
            {(agents || []).map((a) => (
              <SelectItem key={a.agent} value={a.agent}>
                {agentName(t, locale, a.agent, a.label_fi, a.label_en)} · {(a.auto || 0) + (a.owner_queue || 0) + (a.blocked || 0)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {filtered && <Button variant="ghost" size="sm" onClick={() => { setAgent(null); setOutcome(null); }}>{t("ap.clear_filters")}</Button>}
        <span className="ml-auto"><Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} /></span>
      </div>
      <Loadable q={q}>
        {(d) => (
          <DataTable rows={d.items} onRowClick={(r) => setSel(r.id)} pageSize={50}
            empty={<EmptyState title={t(filtered ? "ap.dec.empty_filtered" : "ap.dec.empty")} body={filtered ? null : t("ap.dec.empty_body")} />}
            columns={[
              { key: "at", header: t("ap.dec.at"), cell: (r) => <span className="whitespace-nowrap tabular">{dateTime(r.at, locale)}</span>, headClassName: "w-32" },
              { key: "agent", header: t("ap.dec.agent"), hideBelow: "md", cell: (r) => <span className="whitespace-nowrap">{agentName(t, locale, r.agent, r.agent_label_fi, r.agent_label_en)}</span>, sort: (r) => r.agent_label_fi || r.agent },
              { key: "title", header: t("ap.dec.what"), sortable: false, className: "min-w-[12rem]", cell: (r) => {
                const subj = subjectText(t, r);
                const first = r.reasons?.[0];
                return (
                  <div className="min-w-0">
                    <div className="break-words">{decisionTitle(t, locale, r)}</div>
                    <div className="text-xs text-ink-tertiary break-words">
                      <span className="md:hidden">{agentName(t, locale, r.agent, r.agent_label_fi, r.agent_label_en)}{subj ? " · " : ""}</span>{subj}
                      {r.confidence !== null && r.confidence !== undefined && <span className="md:hidden"> · {t("ap.dec.confidence_inline", { v: num(r.confidence, locale, 2) })}</span>}
                    </div>
                    {first && <div className="mt-0.5 line-clamp-2 text-xs text-ink-secondary">{pick(locale, first.fi, first.en)}{r.reasons.length > 1 ? ` (+${r.reasons.length - 1})` : ""}</div>}
                    <StatusBadge size="sm" className="mt-1 md:hidden" state={OUTCOME_STATE[r.outcome] || "expired"} label={t(`ap.outcome.${r.outcome}`)} />
                  </div>
                );
              } },
              { key: "confidence", header: t("ap.dec.confidence_short"), align: "right", hideBelow: "md", cell: (r) => r.confidence === null || r.confidence === undefined ? <span className="text-ink-tertiary">—</span> : num(r.confidence, locale, 2) },
              { key: "model", header: t("ap.dec.model"), hideBelow: "lg", cell: (r) => r.model ? <span className="font-mono text-xs">{r.model}</span> : <span className="text-ink-tertiary">—</span> },
              { key: "cost", header: t("ap.dec.cost_short"), align: "right", hideBelow: "md", sort: (r) => (r.cost_cents || 0) + (r.spend_cents || 0), headTitle: t("ap.dec.cost_title"),
                cell: (r) => money((r.cost_cents || 0) + (r.spend_cents || 0), locale, { decimals: 2 }) },
              { key: "outcome", header: t("ap.dec.outcome"), hideBelow: "md", cell: (r) => <StatusBadge size="sm" state={OUTCOME_STATE[r.outcome] || "expired"} label={t(`ap.outcome.${r.outcome}`)} /> },
            ]} />
        )}
      </Loadable>
      <DecisionSheet id={sel} onClose={() => setSel(null)} legal={legal} />
    </div>
  );
}
