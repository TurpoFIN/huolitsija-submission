// Customer view of one import declaration. Decision first: what is declared, what it costs in duty and VAT, and what we need from you.
import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import { AlertTriangle, CheckCircle2, Clock, Download, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, Freshness, LoadingRows } from "@/components/app/states";
import { fileUrl } from "@/lib/api";
import { dateTime, money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import ItemsTable from "@/features/customs/ItemsTable";
import StatusTimeline from "@/features/customs/StatusTimeline";
import Totals from "@/features/customs/Totals";
import useDeclaration from "@/features/customs/useDeclaration";
import Validations from "@/features/customs/Validations";
import { ProvenanceLine } from "@/features/customs/common";

function Banner({ tone, icon: Icon, title, body, children }) {
  return (
    <div className={cn("mb-6 flex flex-col gap-3 rounded-xl px-4 py-3 sm:flex-row sm:items-center sm:justify-between",
      tone === "success" ? "status-success" : tone === "danger" ? "status-failed" : tone === "pending" ? "status-pending" : "surface-sunken")}>
      <div className="flex min-w-0 items-start gap-2.5">
        {Icon && <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />}
        <div className="min-w-0">
          <p className="text-sm font-medium">{title}</p>
          {body && <div className="mt-0.5 text-sm opacity-90">{body}</div>}
        </div>
      </div>
      {children && <div className="flex shrink-0 flex-wrap items-center gap-2">{children}</div>}
    </div>
  );
}

function ConfirmDialog({ decl, open, onOpenChange, onConfirm, busy }) {
  const { t, locale } = useT();
  const tot = decl.totals || {};
  return (
    <Dialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t("customs.portal.confirm_title")}</DialogTitle>
          <DialogDescription>{t("customs.portal.confirm_body", { rep: t(`customs.rep.${decl.representation_type || "direct"}`).toLowerCase() })}</DialogDescription>
        </DialogHeader>
        <dl className="surface-sunken rounded-lg px-3 py-2 text-sm">
          <div className="flex justify-between py-0.5"><dt className="text-ink-secondary">{t("customs.totals.customs_value")}</dt><dd className="tabular">{money(tot.customs_value_cents, locale, { decimals: 2 })}</dd></div>
          <div className="flex justify-between py-0.5"><dt className="text-ink-secondary">{t("customs.totals.duty")}</dt><dd className="tabular">{money(tot.duty_cents, locale, { decimals: 2 })}</dd></div>
          <div className="flex justify-between py-0.5"><dt className="text-ink-secondary">{t("customs.totals.vat")}</dt><dd className="tabular">{money(tot.vat_cents, locale, { decimals: 2 })}</dd></div>
          {decl.vat_route && <p className="pt-1 text-xs text-ink-tertiary">{t(`customs.totals.route.${decl.vat_route}`)}</p>}
        </dl>
        <p className="text-sm text-ink-secondary">{t("customs.portal.confirm_after")}</p>
        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>{t("action.cancel")}</Button>
          <Button onClick={onConfirm} disabled={busy}>{busy ? t("state.saving") : t("customs.portal.confirm")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function NextStep({ decl, missing, onConfirm }) {
  const { t, locale } = useT();
  const st = decl.status;
  if (decl.can?.confirm) {
    if (missing.length) {
      return <Banner tone="pending" icon={AlertTriangle} title={t("customs.portal.next.missing", { n: missing.length })}
        body={t("customs.portal.edit_hint")}>
        <Button disabled>{t("customs.portal.confirm")}</Button>
      </Banner>;
    }
    return (
      <Banner tone="pending" icon={AlertTriangle} title={t("customs.portal.next.confirm")} body={t("customs.portal.next.confirm_body")}>
        <Button onClick={onConfirm}>{t("customs.portal.confirm")}</Button>
      </Banner>
    );
  }
  const mrn = decl.mrn ? <span className="font-mono">MRN {decl.mrn}</span> : null;
  if (st === "released" || st === "closed") return <Banner tone="success" icon={CheckCircle2} title={t("customs.portal.next.released", { time: dateTime(decl.released_at, locale) })} body={mrn} />;
  if (st === "accepted") return <Banner icon={CheckCircle2} title={t("customs.portal.next.accepted")} body={mrn} />;
  if (st === "under_control") return <Banner tone="pending" icon={Clock} title={t("customs.portal.next.control")} body={mrn} />;
  if (st === "rejected") return <Banner tone="pending" icon={Clock} title={t("customs.portal.next.rejected")} />;
  if (st === "not_released") return <Banner tone="danger" icon={XCircle} title={t("customs.portal.next.not_released")} body={mrn} />;
  if (["approved", "submitted"].includes(st)) return <Banner icon={Clock} title={t("customs.portal.next.filing")} body={mrn} />;
  return <Banner icon={Clock} title={t("customs.portal.next.review")} />;
}

export default function PortalCustomsDetail() {
  const { id } = useParams();
  const { t } = useT();
  const { q, decl, patch, act, busy, saveState } = useDeclaration(id);
  const [confirmOpen, setConfirmOpen] = useState(false);

  if (q.loading && !decl) {
    return <div aria-busy="true"><Skeleton className="mb-3 h-7 w-64" /><Skeleton className="mb-6 h-14 w-full rounded-xl" /><LoadingRows rows={6} /></div>;
  }
  if (q.error && !decl) return <ErrorState error={q.error} onRetry={q.reload} />;
  if (!decl) return null;

  const can = decl.can || {};
  const mine = (decl.validations || []).filter((v) => v.owner === "customer");
  const missing = mine.filter((v) => !v.ok && v.severity === "blocking");
  const confirm = async () => {
    try {
      await act("confirm", `/customs/${id}/submit-for-review`, undefined, t("customs.toast.confirmed"));
      setConfirmOpen(false);
    } catch (e) { /* toast shown; dialog stays open */ }
  };

  const meta = (
    <>
      <StatusBadge group="customs" value={decl.status} />
      {decl.mrn && <span className="font-mono text-[13px]" title={t("customs.portal.mrn_hint")}>MRN {decl.mrn}</span>}
      {decl.shipment && <Link to={`/portal/shipments/${decl.shipment.id}`} className="text-ink-accent hover:underline">{t("customs.action.open_shipment")}</Link>}
      <span>{t(`customs.rep.${decl.representation_type || "direct"}`)}</span>
      <span className="font-mono text-xs text-ink-tertiary">{decl.lrn}</span>
      <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
    </>
  );

  return (
    <div>
      <PageHeader crumbs={[{ label: t("customs.portal.title"), to: "/portal/customs" }, { label: decl.lrn }]}
        title={decl.shipment?.reference ? `${t("customs.panel.title")} · ${decl.shipment.reference}` : decl.lrn} meta={meta}
        actions={<Button variant="outline" size="sm" asChild><a href={fileUrl(`/customs/${id}/pdf?download=1`)}><Download /> {t("customs.action.pdf")}</a></Button>} />
      <ProvenanceLine decl={decl} className="-mt-2 mb-4" />
      {q.error && <ErrorState className="mb-3" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}

      <NextStep decl={decl} missing={missing} onConfirm={() => setConfirmOpen(true)} />

      <div className="grid gap-x-8 lg:grid-cols-[minmax(0,1fr)_300px] lg:grid-rows-[auto_1fr]">
        <aside className="lg:col-start-2 lg:row-start-1">
          <Section title={t("customs.portal.what")} actions={<span className="text-xs text-ink-tertiary">{t("customs.estimate")}</span>}>
            <Totals decl={decl} />
          </Section>
        </aside>
        <div className="min-w-0 lg:col-start-1 lg:row-span-2 lg:row-start-1">
          <Section title={t("customs.portal.your_checks")}
            actions={<span className="text-xs text-ink-tertiary">{missing.length ? t("customs.blocking_n", { n: missing.length }) : t("customs.portal.no_checks")}</span>}>
            <Validations validations={mine} owners={["customer"]} />
          </Section>
          <Section title={t("customs.sec.items")}
            actions={<span className="text-xs text-ink-tertiary">{can.edit ? t("customs.portal.edit_hint") : t("customs.portal.locked")}</span>}>
            <ItemsTable decl={decl} mode="portal" canEdit={can.edit} onPatch={patch} saveState={saveState} />
          </Section>
        </div>
        <aside className="lg:col-start-2 lg:row-start-2">
          <Section title={t("customs.sec.timeline")}>
            <StatusTimeline decl={decl} />
          </Section>
        </aside>
      </div>
      <ConfirmDialog decl={decl} open={confirmOpen} onOpenChange={setConfirmOpen} onConfirm={confirm} busy={busy === "confirm"} />
    </div>
  );
}
