import DomainSettings from "@/features/DomainSettings";
import React, { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { PageHeader, Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { StatusBadge } from "@/components/app/StatusBadge";
import { Loadable } from "@/components/app/states";
import { ProfileSettings } from "@/features/ProfileSettings";
import { apiPatch, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { toast } from "sonner";

const NUM = ["quote_approval_threshold_eur", "default_margin_pct", "customs_clearance_fee_eur", "customs_extra_item_fee_eur", "documentation_fee_eur",
  "payment_terms_days", "late_interest_pct", "quote_validity_days", "insurance_rate_pct", "insurance_min_premium_eur", "vat_rate_standard_pct", "vat_rate_reduced_pct", "vat_rate_reduced_low_pct", "record_retention_years"];
const BOOL = ["ai_reply_requires_approval", "ai_auto_send_routine_messages", "auto_approve_low_risk_kyc", "sms_notifications_enabled"];
const MODE_STATE = { live: "success", enabled: "success", test: "scheduled", configured: "in-progress", not_configured: "pending", log_only: "pending", disabled: "expired" };

function Row({ label, children }) {
  return (
    <div className="flex flex-col gap-2 border-b border-[color:var(--border-subtle)] py-3 sm:flex-row sm:items-center sm:justify-between">
      <span className="text-sm">{label}</span><div className="sm:w-56">{children}</div>
    </div>
  );
}

function Business() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const q = useApi("/settings");
  const [form, setForm] = useState(null);
  useEffect(() => { if (q.data) setForm(Object.fromEntries(Object.entries(q.data).map(([k,v]) => [k, typeof v === "number" ? String(v).replace(".", locale === "fi" ? "," : ".") : v]))); }, [q.data, locale]);
  const save = async () => {
    const body = {};
    for (const k of NUM) {
      const raw = String(form[k] ?? "").trim().replace(",", ".");
      if (!raw || !/^\d+(?:\.\d+)?$/.test(raw) || !Number.isFinite(Number(raw))) { toast.error(`${t(`osettings.f.${k}`)}: ${t("review.invalid")}`); return; }
      body[k] = Number(raw);
    }
    BOOL.forEach((k) => { body[k] = !!form[k]; });
    try { await apiPatch("/settings", body); toast.success(t("osettings.saved")); q.reload(); } catch (e) { toast.error(e.message); }
  };
  return (
    <Loadable q={q}>
      {() => form && (
        <>
          {NUM.map((k) => <Row key={k} label={t(`osettings.f.${k}`)}><Input type="text" inputMode="decimal" aria-label={t(`osettings.f.${k}`)} className="text-right tabular" value={form[k] ?? ""} disabled={!can("settings.write")} onChange={(e) => setForm({ ...form, [k]: e.target.value })} /></Row>)}
          {BOOL.map((k) => <Row key={k} label={t(`osettings.f.${k}`)}><Switch checked={!!form[k]} disabled={!can("settings.write")} onCheckedChange={(v) => setForm({ ...form, [k]: v })} aria-label={t(`osettings.f.${k}`)} /></Row>)}
          <Row label={t("osettings.f.terms_version")}><span className="text-sm text-ink-secondary">{form.terms_version}</span></Row>
          {can("settings.write") && <div className="mt-3"><Button onClick={save}>{t("action.save")}</Button></div>}
        </>
      )}
    </Loadable>
  );
}

function Integrations() {
  const { t } = useT();
  const q = useApi("/integrations/status");
  return (
    <Loadable q={q}>
      {(d) => {
        const rows = [
          { k: "stripe", mode: d.stripe.mode, detail: d.stripe.webhook ? `${t("review.webhook")}: ${t("review.configured")}` : `${t("review.webhook")}: ${t("review.not_configured")}` },
          { k: "email", mode: d.email.mode, detail: `${d.email.provider === "log" ? t("osettings.mode.log_only") : d.email.provider} · ${d.email.from}` },
          ...(d.sms ? [{ k: "sms", mode: d.sms.mode, detail: `${d.sms.provider === "log" ? t("osettings.mode.log_only") : d.sms.provider} · ${d.sms.enabled ? t("channels.sms_on") : t("channels.sms_off")}` }] : []),
          { k: "ai", mode: d.ai.available ? "enabled" : "not_configured", detail: `${d.ai.provider === "none" ? t("review.ai.no_provider") : d.ai.provider} · ${d.ai.model}` },
          { k: "tulli", mode: d.tulli.mode, detail: d.tulli.endpoint_set ? `${t("review.endpoint")}: ${t("review.configured")}` : `${t("review.endpoint")}: ${t("review.not_configured")}` },
          { k: "digitraffic", mode: d.digitraffic.enabled ? "live" : "disabled", detail: "meri.digitraffic.fi · rata.digitraffic.fi" },
          { k: "company", mode: d.company.business_id ? "configured" : "not_configured", detail: [d.company.business_id, d.company.vat_id, d.company.eori].filter(Boolean).join(" · ") || t("review.business_ids") },
        ];
        return (
          <>
            <DataTable rows={rows} rowKey="k" columns={[
              { key: "k", header: "", cell: (r) => <span className="font-medium">{t(`osettings.i.${r.k}`)}</span> },
              { key: "mode", header: t("field.status"), cell: (r) => <StatusBadge size="sm" state={MODE_STATE[r.mode] || "expired"} label={t(`osettings.mode.${r.mode}`)} /> },
              { key: "detail", header: "", cell: (r) => <span className="text-xs text-ink-tertiary">{r.detail}</span>, hideBelow: "md" },
            ]} />
            <p className="mt-2 text-xs text-ink-tertiary">{t("review.setup_help")}</p>
          </>
        );
      }}
    </Loadable>
  );
}

function Outbox() {
  const { t, locale } = useT();
  const q = useApi("/ops/email-outbox?limit=50");
  return (
    <Loadable q={q}>
      {(d) => (
        <DataTable rows={d.items} empty={<p className="text-sm text-ink-tertiary">{t("state.empty")}</p>} columns={[
          { key: "created_at", header: t("field.created"), cell: (m) => dateTime(m.created_at, locale) },
          { key: "to", header: t("review.to"), cell: (m) => <span className="text-ink-secondary">{m.to}</span>, hideBelow: "md" },
          { key: "subject", header: t("review.subject") },
          { key: "status", header: t("field.status"), cell: (m) => <StatusBadge size="sm" group="generic" value={m.status === "sent" ? "ok" : m.status === "failed" ? "error" : "warning"} label={t(`review.email.${m.status}`)} /> },
        ]} />
      )}
    </Loadable>
  );
}

export default function OpsSettings() {
  const { t } = useT();
  const { can } = useAuth();
  return (
    <div className="max-w-[1024px]">
      <PageHeader title={t("osettings.title")} />
      <DomainSettings />
      <Section title={t("osettings.business")}><Business /></Section>
      <Section title={t("osettings.integrations")}><Integrations /></Section>
      {can("settings.write") && <Section title={t("osettings.outbox")}><p className="mb-2 text-xs text-ink-tertiary">{t("osettings.outbox_hint")}</p><Outbox /></Section>}
      <ProfileSettings showNotifications={false} />
    </div>
  );
}
