// Customer company profile. Decision: what is still missing before we can verify the account and clear goods.
import React, { useState } from "react";
import { AlertTriangle, ArrowRight, CheckCircle2, Eye, XCircle } from "lucide-react";
import { toast } from "sonner";
import { PageHeader, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, LoadingRows, Loadable } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { KycChecklist, RunChecksAction, checksNeverRun } from "@/features/company/KycChecklist";
import { ProfileForm } from "@/features/company/ProfileForm";
import { SignatoryForm, UboEditor } from "@/features/company/UboEditor";
import { PoaSign } from "@/features/company/PoaSign";
import { PoaBadge, ytunnusValid } from "@/features/company/common";

/** What the customer still has to do, in order. Each item links to its section. */
function todoItems(c, t) {
  const items = [];
  const by = Object.fromEntries((c.kyc_checklist || []).map((x) => [x.key, x]));
  if (!c.business_id || !ytunnusValid(c.business_id)) items.push({ to: "#details", label: t("company.todo.business_id") });
  if (checksNeverRun(c.kyc_checklist) || c.kyc_stale) items.push({ to: "#verification", label: t("company.todo.run") });
  // Once compliance approved the account, register mismatches are settled; do not ask the customer again.
  for (const k of c.kyc_status === "approved" ? [] : ["ytunnus", "ytj", "vies"]) {
    if (["fail", "error"].includes(by[k]?.status)) items.push({ to: "#verification", label: t("company.todo.fix", { check: t(`company.check.${k}`) }) });
  }
  if (!c.ubo_none_declared && !(c.ubos || []).length) items.push({ to: "#owners", label: t("company.todo.ubo") });
  if (!c.poa_valid) items.push({ to: "#poa", label: t("company.todo.poa") });
  if (!c.eori) items.push({ to: "#details", label: t("company.todo.eori") });
  else if (["fail", "error"].includes(by.eori?.status)) items.push({ to: "#details", label: t("company.todo.fix", { check: t("company.check.eori") }) });
  return items;
}

function NextSteps({ c }) {
  const { t } = useT();
  const items = todoItems(c, t);
  const status = c.kyc_status;
  let line = null;
  if (status === "rejected") line = { Icon: XCircle, cls: "status-failed", text: t("company.todo.rejected") };
  else if (status === "in_review") line = { Icon: Eye, cls: "status-in-review", text: t("company.todo.review") };
  else if (status === "approved" && !items.length) line = { Icon: CheckCircle2, cls: "status-success", text: t("company.ready") };
  else if (status === "approved") line = { Icon: CheckCircle2, cls: "status-success", text: t("company.ready_no_customs") };
  if (!line && !items.length) return null;
  return (
    <div className="mb-6 flex flex-col gap-2">
      {line && (
        <p className={cn("flex items-start gap-2 rounded-lg px-3 py-2 text-sm", line.cls)}>
          <line.Icon className="mt-0.5 size-4 shrink-0" aria-hidden /> {line.text}
        </p>
      )}
      {items.length > 0 && (
        <div className="rounded-lg border px-3 py-2">
          <p className="mb-1 text-xs text-ink-tertiary">{t("company.todo.title")}</p>
          <ul className="flex flex-col sm:flex-row sm:flex-wrap sm:gap-x-5">
            {items.map((it, i) => (
              <li key={i}>
                <a href={it.to} className="inline-flex min-h-8 items-center gap-1.5 text-sm hover:underline">
                  <ArrowRight className="size-3.5 text-ink-tertiary" aria-hidden /> {it.label}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function CompanyBody({ q }) {
  const { t } = useT();
  const { can } = useAuth();
  const canManage = can("company.manage");
  const c = q.data;
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState(null);
  const update = (next) => q.setData((prev) => ({ ...prev, ...next }));

  const runChecks = async () => {
    setRunning(true);
    setRunError(null);
    try {
      const res = await apiPost("/company/kyc/run", {});
      q.setData(res);
      toast.success(t("company.checks.done"));
    } catch (e) {
      setRunError(e);
      q.reload();
    } finally {
      setRunning(false);
    }
  };
  const bidOk = c.business_id && ytunnusValid(c.business_id);
  const checkedAt = checksNeverRun(c.kyc_checklist) ? null : c.kyc_checked_at || c.kyc?.run_at;

  return (
    <>
      <PageHeader
        title={c.name || t("company.title")}
        meta={<>
          <span className="font-mono text-[13px]">{c.business_id ? t("company.business_id", { id: c.business_id }) : t("company.no_business_id")}</span>
          <StatusBadge group="kyc" value={c.kyc_status || "not_started"} size="sm" />
          <PoaBadge poa={c.poa} valid={c.poa_valid} />
        </>}
      />
      <NextSteps c={c} />
      <div className="grid gap-x-8 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0">
          <Section id="verification" title={t("company.checks.title")}
            actions={<RunChecksAction checkedAt={checkedAt} running={running} onRun={runChecks} canRun={canManage}
              disabledReason={!bidOk ? t("company.checks.need_bid") : null} />}>
            {c.kyc_stale && !running && (
              <p className="mb-2 flex items-start gap-2 rounded-lg px-3 py-2 text-sm status-pending">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden /> {t("company.stale")}
              </p>
            )}
            {running && <p className="mb-1 text-xs text-ink-tertiary" aria-live="polite">{t("company.checks.running_hint")}</p>}
            {!running && checksNeverRun(c.kyc_checklist) && <p className="mb-1 text-sm text-ink-tertiary">{t("company.checks.never_hint")}</p>}
            {runError && <ErrorState className="mb-2" compact error={runError} onRetry={runChecks} />}
            <KycChecklist checklist={c.kyc_checklist} audience="customer" running={running} />
          </Section>
          <Section id="details" title={t("company.profile.title")}>
            <ProfileForm company={c} canEdit={canManage} onSaved={update} />
          </Section>
          <Section id="owners" title={t("company.ubo.title")}>
            <UboEditor company={c} canEdit={canManage} onSaved={update} />
          </Section>
          <Section id="signatory" title={t("company.signatory.title")}>
            <SignatoryForm company={c} canEdit={canManage} onSaved={update} />
          </Section>
        </div>
        <div className="min-w-0">
          <Section id="poa" title={t("company.poa.title")}>
            <PoaSign company={c} canSign={canManage} onChange={(res) => q.setData(res)} />
          </Section>
        </div>
      </div>
    </>
  );
}

export default function Company() {
  const q = useApi("/company");
  return (
    <div className="max-w-[1152px]">
      <Loadable q={q} skeleton={<LoadingRows rows={8} />}>
        {() => <CompanyBody q={q} />}
      </Loadable>
    </div>
  );
}
