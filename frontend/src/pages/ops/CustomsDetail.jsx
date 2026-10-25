// Broker workbench for one import declaration. Decision first: what stands between this declaration and filing / release.
import CustomsDecision, { TradeDefencePanel } from "@/features/customs/CustomsDecision";
import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import { CheckCircle2, Download, FileText, Loader2, RefreshCw, Sparkles, UserMinus, UserPlus, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, Freshness, LoadingRows } from "@/components/app/states";
import { apiPost, fileUrl } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import ApprovalDialog from "@/features/customs/ApprovalDialog";
import { Submission, XmlPreview } from "@/features/customs/Filing";
import ItemsTable from "@/features/customs/ItemsTable";
import Parties from "@/features/customs/Parties";
import RecordResponseDialog, { RESPONSES } from "@/features/customs/RecordResponseDialog";
import StatusTimeline from "@/features/customs/StatusTimeline";
import Totals from "@/features/customs/Totals";
import useDeclaration from "@/features/customs/useDeclaration";
import Validations from "@/features/customs/Validations";
import Valuation from "@/features/customs/Valuation";
import { ProvenanceLine } from "@/features/customs/common";

import { CustomsHubTabs } from "@/features/customs/extCommon";
import TaricDutyBasis from "@/features/customs/TaricDutyBasis";
import { LicenceRequirements } from "@/features/customs/TaricLicences";
import OriginPreference from "@/features/customs/OriginPreference";
import ControlsDeclaration, { ScreeningPanel } from "@/features/customs/ControlsDeclaration";

import TaricBti from "@/features/customs/TaricBti";

const PRE_FILING = ["draft", "needs_info", "ready_for_review", "rejected"];

function Banner({ tone = "neutral", icon: Icon, title, body, children }) {
  return (
    <div className={cn("mb-6 flex flex-col gap-3 rounded-xl px-4 py-3 sm:flex-row sm:items-center sm:justify-between",
      tone === "neutral" ? "surface-sunken" : tone === "success" ? "status-success" : tone === "danger" ? "status-failed" : "status-pending")}>
      <div className="flex min-w-0 items-start gap-2.5">
        {Icon && <Icon className={cn("mt-0.5 size-4 shrink-0", Icon === Loader2 && "animate-spin")} aria-hidden />}
        <div className="min-w-0">
          <p className="text-sm font-medium">{title}</p>
          {body && <div className="mt-0.5 text-sm opacity-90">{body}</div>}
        </div>
      </div>
      {children && <div className="flex shrink-0 flex-wrap items-center gap-2">{children}</div>}
    </div>
  );
}

function Why({ children }) {
  return children ? <span className="text-xs text-ink-tertiary">{children}</span> : null;
}

function NextStep({ decl, blocking, busy, onRequest, onDecide, onRecord }) {
  const { t, locale } = useT();
  const can = decl.can || {};
  const st = decl.status;
  const ap = decl.approval;
  const nB = blocking.filter((v) => (v.owner || "broker") === "broker").length;
  const nC = blocking.filter((v) => v.owner === "customer").length;

  if (decl.drafting) return <Banner icon={Loader2} title={t("customs.next.drafting")} body={relTime(decl.drafting.since, locale)} />;

  if (PRE_FILING.includes(st)) {
    if (ap?.status === "pending") {
      return (
        <Banner tone="pending" icon={CheckCircle2} title={t("customs.next.approval_pending")}
          body={<>{t("customs.next.approval_pending_body", { time: relTime(ap.created_at, locale) })} {t("customs.next.edit_voids")}</>}>
          {can.decide_approval ? (
            <>
              <Button variant="ghost" onClick={() => onDecide("rejected")}>{t("action.reject")}</Button>
              <Button onClick={() => onDecide("approved")}>{t("customs.action.approve_file")}</Button>
            </>
          ) : <Why>{t("customs.why.perm")}</Why>}
        </Banner>
      );
    }
    const extra = [
      decl.approval_voided?.at ? t("customs.next.voided_detail", { time: relTime(decl.approval_voided.at, locale),
        reason: decl.approval_voided.reason || t("customs.next.voided") }) : null,
      st === "rejected" ? t("customs.next.rejected") : null,
      st === "draft" && !decl.confirmation ? t("customs.next.customer_pending") : null,
    ].filter(Boolean);
    if (blocking.length) {
      return (
        <Banner icon={XCircle} title={blocking.length === 1 ? t("customs.next.blocking_1") : t("customs.next.blocking", { n: blocking.length })}
          body={<>{t("customs.next.split", { broker: nB, customer: nC })}{extra.map((x) => <span key={x} className="block">{x}</span>)}</>}>
          <Why>{t("customs.why.blocking", { n: blocking.length })}</Why>
          <Button disabled>{t("customs.action.request_approval")}</Button>
        </Banner>
      );
    }
    return (
      <Banner icon={CheckCircle2} title={t("customs.next.ready")}
        body={<>{t("customs.next.ready_body")}{extra.map((x) => <span key={x} className="block">{x}</span>)}</>}>
        {!can.request_approval && <Why>{t("customs.why.perm")}</Why>}
        <Button onClick={onRequest} disabled={!can.request_approval || busy === "approve"}>
          {busy === "approve" ? t("state.saving") : t("customs.action.request_approval")}
        </Button>
      </Banner>
    );
  }

  const record = can.record_response && (RESPONSES[st] || []).length > 0 ? (
    <Button onClick={onRecord}>{t("customs.action.record_response")}</Button>
  ) : null;
  if (st === "approved") {
    const manual = decl.submission?.state === "awaiting_manual_filing" || decl.gateway?.channel === "manual";
    return (
      <Banner tone="pending" icon={FileText} title={manual ? t("customs.next.manual_file") : tEnum(t, "customs", st)} body={decl.submission?.note}>
        <Button variant="outline" asChild><a href={fileUrl(`/customs/${decl.id}/pdf`)} target="_blank" rel="noreferrer"><FileText /> {t("customs.action.pdf")}</a></Button>
        {record}
      </Banner>
    );
  }
  if (st === "released") {
    return <Banner tone="success" icon={CheckCircle2} title={t("customs.next.released", { time: dateTime(decl.released_at, locale) })}
      body={decl.mrn ? <span className="font-mono">MRN {decl.mrn}</span> : null}>{record && <Button variant="outline" onClick={onRecord}>{t("customs.action.record_response")}</Button>}</Banner>;
  }
  const key = { submitted: "submitted", accepted: "accepted", under_control: "under_control", not_released: "not_released", closed: "closed" }[st];
  return (
    <Banner tone={st === "not_released" ? "danger" : "pending"} icon={st === "not_released" ? XCircle : FileText}
      title={key ? t(`customs.next.${key}`) : tEnum(t, "customs", st)} body={decl.mrn ? <span className="font-mono">MRN {decl.mrn}</span> : null}>
      {record}
    </Banner>
  );
}

function DetailSkeleton() {
  return (
    <div aria-busy="true">
      <Skeleton className="mb-2 h-3 w-40" />
      <Skeleton className="mb-3 h-7 w-64" />
      <Skeleton className="mb-6 h-14 w-full rounded-xl" />
      <LoadingRows rows={8} />
    </div>
  );
}

export default function OpsCustomsDetail() {
  const { id } = useParams();
  const { t, locale } = useT();
  const { user } = useAuth();
  const { q, decl, patch, act, busy, saveState, setData } = useDeclaration(id);
  const [rrOpen, setRrOpen] = useState(false);
  const [decision, setDecision] = useState(null);
  const [focusSeq, setFocusSeq] = useState(null);
  const [reclassifying, setReclassifying] = useState(null);

  if (q.loading && !decl) return <DetailSkeleton />;
  if (q.error && !decl) return <ErrorState error={q.error} onRetry={q.reload} />;
  if (!decl) return null;

  const can = decl.can || {};
  const blocking = (decl.validations || []).filter((v) => !v.ok && v.severity === "blocking");
  const shipment = decl.shipment;
  const mine = decl.assignee?.id && decl.assignee.id === user?.id;
  const redraftable = ["draft", "needs_info"].includes(decl.status);

  const onRequest = () => act("approve", `/customs/${id}/approve`, undefined, t("customs.toast.approval_requested")).catch(() => {});
  const onReclassify = async (seq) => {
    setReclassifying(seq);
    try { await act("classify", `/customs/${id}/items/${seq}/classify`); } catch (e) { /* toast shown */ } finally { setReclassifying(null); }
  };
  const onDecided = async (d) => {
    toast.success(d === "approved" ? t("customs.toast.approved") : t("action.reject"));
    await q.reload();
  };

  const meta = (
    <>
      <StatusBadge group="customs" value={decl.status} />
      {decl.mrn && <span className="font-mono text-[13px]">MRN {decl.mrn}</span>}
      {shipment && <Link to={`/ops/shipments/${shipment.id}`} className="hover:text-foreground hover:underline">{shipment.reference}</Link>}
      <span>{decl.company_name}</span>
      <span>{t(`customs.rep.${decl.representation_type || "direct"}`)}</span>
      <span className="text-ink-tertiary">{t("customs.type")}</span>
      {decl.demo && <span className="text-xs text-ink-tertiary">{t("customs.demo")}</span>}
      <span className="text-ink-tertiary">{t("customs.col.owner")}: {decl.assignee?.name || t("customs.unassigned")}</span>
      <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
    </>
  );

  const actions = (
    <>
      {can.edit && (
        <Button variant="ghost" size="sm" disabled={busy === "assign"}
          onClick={() => act("assign", `/customs/${id}/assign`, { user_id: mine ? null : user?.id }, t("customs.toast.assigned")).catch(() => {})}>
          {mine ? <UserMinus /> : <UserPlus />} {mine ? t("customs.action.unassign") : t("customs.action.assign_me")}
        </Button>
      )}
      {redraftable && can.edit && (
        <Button variant="outline" size="sm" disabled={!!decl.drafting || busy === "redraft"} title={t("customs.action.redraft")}
          onClick={() => act("redraft", `/customs/${id}/redraft`, undefined, t("customs.toast.redraft")).catch(() => {})}>
          <Sparkles /> {t("customs.action.redraft")}
        </Button>
      )}
      {can.file && (
        <Button variant="outline" size="sm" disabled={busy === "file"}
          onClick={() => act("file", `/customs/${id}/file`, undefined, t("customs.toast.filed")).catch(() => {})}>
          <RefreshCw /> {t("customs.action.file_again")}
        </Button>
      )}
      <Button variant="outline" size="sm" asChild>
        <a href={fileUrl(`/customs/${id}/pdf?download=1`)}><Download /> {t("customs.action.pdf")}</a>
      </Button>
    </>
  );

  return (
    <div>
      <PageHeader crumbs={[{ label: t("customs.title"), to: "/ops/customs" }, { label: decl.lrn }]}
        title={<span className="font-mono">{decl.lrn}</span>} meta={meta} actions={actions} />
      <CustomsHubTabs active="import" />
      {decl.ai_drafted !== false && <ProvenanceLine decl={decl} className="-mt-2 mb-4" />}
      {q.error && <ErrorState className="mb-3" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}

      <NextStep decl={decl} blocking={blocking} busy={busy} onRequest={onRequest}
        onDecide={setDecision} onRecord={() => setRrOpen(true)} />

      <CustomsDecision decl={decl} onSaved={q.reload} />
      <TradeDefencePanel decl={decl} patch={patch} />
      <div className="grid gap-x-8 lg:grid-cols-[minmax(0,1fr)_300px] lg:grid-rows-[auto_1fr]">
        <aside className="lg:col-start-2 lg:row-start-1">
          <Section title={t("customs.sec.totals")} actions={<span className="text-xs text-ink-tertiary">{t("customs.estimate")}</span>}>
            <Totals decl={decl} />
          </Section>
        </aside>

        <div className="min-w-0 lg:col-start-1 lg:row-span-2 lg:row-start-1">
          <Section title={t("customs.sec.validations")}
            actions={<span className="text-xs tabular text-ink-tertiary">{blocking.length ? t("customs.blocking_n", { n: blocking.length }) : t("customs.checks_ok")}</span>}>
            <Validations validations={decl.validations} onItem={(seq) => setFocusSeq(seq)} actionFor={(v) => (
              // EU EOS did not find the EORI: the audited manual verification lives on the customer profile.
              v.rule === "E-1" && !v.ok && /registry/i.test(v.message || "") && decl.company_id ? (
                <Link to={`/ops/customers/${decl.company_id}`} className="shrink-0 text-xs text-ink-accent hover:underline">
                  {t("customs.action.verify_eori")}
                </Link>
              ) : null)} />
          </Section>
          <Section id="items" title={t("customs.sec.items")}
            actions={<span className="text-xs text-ink-tertiary">{(decl.items || []).length === 1 ? t("customs.items_1") : t("customs.items_n", { n: (decl.items || []).length })}</span>}>
            <ItemsTable decl={decl} mode="ops" canEdit={can.edit} onPatch={patch} saveState={saveState}
              onReclassify={can.edit ? onReclassify : null} reclassifying={reclassifying} focusSeq={focusSeq} />
          </Section>
          <Section title={t("cx.duty.title")}><TaricDutyBasis decl={decl} /></Section>
          {[...new Set((decl.items || []).map(it => it.taric_code || it.cn_code).filter(Boolean))].map(code => <TaricBti key={code} code={code} />)}
          <Section title={t("cx.origin.title")}><OriginPreference decl={decl} /></Section>
          <Section title={t("cx.lic.title")}><LicenceRequirements decl={decl} /></Section>
          <Section title={t("cx.screen.title")}><ScreeningPanel decl={decl} busy={busy === "screen"} onScreen={can.edit ? () => act("screen", `/customs/${id}/screen`).catch(() => {}) : null} /></Section>
          <Section title={t("customs.sec.valuation")}>
            <Valuation decl={decl} canEdit={can.edit} onPatch={patch} saveState={saveState("valuation")} />
          </Section>
          <Section title={t("customs.sec.parties")}>
            <Parties decl={decl} canEdit={can.edit} onPatch={patch} saveState={saveState("parties")} screening={busy === "screen"}
              onScreen={can.edit ? () => act("screen", `/customs/${id}/screen`, undefined, t("customs.toast.screened")).catch(() => {}) : null} />
          </Section>
          <Section title={t("customs.sec.xml")}>
            <XmlPreview decl={decl} />
          </Section>
        </div>

        <aside className="lg:col-start-2 lg:row-start-2">
          <Section title={t("cx.ctl.title")}><ControlsDeclaration id={id} /></Section>
          <Section title={t("customs.sec.submission")}>
            <Submission decl={decl} />
            {decl.approval && decl.approval.status !== "pending" && (
              <p className="mt-2 text-xs text-ink-tertiary">
                {tEnum(t, "approval", decl.approval.status)} · {decl.approval.decided_by?.name || "—"} · {dateTime(decl.approval.decided_at, locale)}
              </p>
            )}
          </Section>
          <Section title={t("customs.sec.timeline")}>
            <StatusTimeline decl={decl} />
          </Section>
          {(decl.documents || []).length > 0 && (
            <Section title={t("customs.sec.documents")}>
              <ul className="flex flex-col gap-1.5">
                {decl.documents.map((d) => (
                  <li key={d.document_id} className="flex items-center justify-between gap-2 text-sm">
                    <a href={fileUrl(`/documents/${d.document_id}/file`)} target="_blank" rel="noreferrer" className="min-w-0 truncate hover:underline">
                      {tEnum(t, "doc", d.type)}
                    </a>
                    <span className="shrink-0 font-mono text-[11px] text-ink-tertiary">{d.code} {d.number}</span>
                  </li>
                ))}
              </ul>
            </Section>
          )}
        </aside>
      </div>

      <RecordResponseDialog decl={decl} open={rrOpen} onOpenChange={setRrOpen}
        onDone={(d) => { setData(d); toast.success(t("customs.toast.recorded")); }} />
      <ApprovalDialog decl={decl} decision={decision} open={!!decision} onOpenChange={(o) => !o && setDecision(null)} onDone={onDecided} />
    </div>
  );
}
