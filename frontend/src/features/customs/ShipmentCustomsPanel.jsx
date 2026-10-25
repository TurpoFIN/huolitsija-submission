// Customs block embedded in a shipment detail page (portal and ops). No page header: Section blocks only.
// props: { shipment, mode: "portal" | "ops", onChange }
import React, { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { ArrowRight, CheckCircle2, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Section } from "@/components/app/layout";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, LoadingRows } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { fmtCode } from "./common";

function Declaration({ s, mode }) {
  const { t, locale } = useT();
  const base = mode === "ops" ? "/ops/customs" : "/portal/customs";
  const blocking = mode === "ops" ? s.blocking_count : s.blocking_customer;
  const codes = [...new Set(s.cn_codes || [])].filter(Boolean);
  const editableByCustomer = mode === "portal" && ["draft", "needs_info"].includes(s.status);
  const amount = (value, complete) => value == null || complete === false
    ? t("customs.totals.not_calculated") : money(value, locale, { decimals: 2 });
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="inline-flex flex-wrap items-center gap-2">
          <StatusBadge group="customs" value={s.status} />
          <span className="font-mono text-[13px]">{s.lrn}</span>
          {s.mrn && <span className="font-mono text-[13px] text-ink-secondary">MRN {s.mrn}</span>}
          {s.ai_drafted && <AgentMark title={t("customs.provenance")} />}
        </span>
        <Button variant={editableByCustomer ? "default" : "outline"} size="sm" asChild>
          <Link to={`${base}/${s.id}`}>{editableByCustomer ? t("customs.portal.confirm") : t("customs.panel.open")} <ArrowRight /></Link>
        </Button>
      </div>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
        <div><dt className="text-xs text-ink-tertiary">{t("customs.totals.customs_value")}</dt><dd className="tabular">{money(s.customs_value_cents, locale, { decimals: 2 })}</dd></div>
        <div><dt className="text-xs text-ink-tertiary">{t("customs.totals.duty")}</dt><dd className="tabular">{amount(s.duty_cents, s.duty_complete)}</dd></div>
        <div><dt className="text-xs text-ink-tertiary">{t("customs.totals.vat")}</dt><dd className="tabular">{amount(s.vat_cents, s.vat_complete)}</dd></div>
        <div><dt className="text-xs text-ink-tertiary">{t("customs.col.checks")}</dt>
          <dd className="inline-flex items-center gap-1">
            {blocking ? <><XCircle className="size-3.5 text-danger-fg" aria-hidden />{t("customs.blocking_n", { n: blocking })}</>
              : <><CheckCircle2 className="size-3.5 text-success-fg" aria-hidden />{t("customs.checks_ok")}</>}
          </dd>
        </div>
      </dl>
      <p className="text-xs text-ink-tertiary">
        {(s.items_count === 1 ? t("customs.items_1") : t("customs.items_n", { n: s.items_count || 0 }))}
        {codes.length > 0 && <> · <span className="font-mono">{codes.slice(0, 4).map(fmtCode).join(" · ")}</span></>}
        {codes.length > 4 && ` ${t("customs.panel.more_items", { n: codes.length - 4 })}`}
        {s.vat_route && ` · ${t(`customs.vat.${s.vat_route}`)}`} · {t("customs.estimate").toLowerCase()}
      </p>
    </div>
  );
}

export default function ShipmentCustomsPanel({ shipment, mode = "portal", onChange }) {
  const { t } = useT();
  const { can } = useAuth();
  const sid = shipment?.id;
  const q = useApi(sid ? `/customs?shipment_id=${encodeURIComponent(sid)}` : null);
  const [busy, setBusy] = useState(false);
  const items = q.data?.items || [];
  const isImport = (shipment?.direction || "import") === "import";

  const draft = async () => {
    setBusy(true);
    try {
      await apiPost("/customs", { shipment_id: sid });
      toast.success(t("customs.toast.drafted"));
      await q.reload();
      onChange?.();
    } catch (e) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title={t("customs.panel.title")}>
      {q.loading && !q.data ? (
        <LoadingRows rows={2} />
      ) : q.error && !q.data ? (
        <ErrorState error={q.error} onRetry={q.reload} compact />
      ) : items.length === 0 ? (
        <EmptyState title={t("customs.panel.none")} body={isImport ? t("customs.panel.none_import") : t("customs.panel.none_export")}
          action={mode === "ops" && isImport && can?.("customs.review") ? (
            <Button size="sm" onClick={draft} disabled={busy}>{busy ? t("state.saving") : t("customs.panel.draft")}</Button>
          ) : null} />
      ) : (
        <div className="flex flex-col divide-y divide-[color:var(--border-subtle)]">
          {q.error && <ErrorState className="mb-2" compact error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
          {items.map((s) => <div key={s.id} className="py-2 first:pt-0 last:pb-0"><Declaration s={s} mode={mode} /></div>)}
        </div>
      )}
    </Section>
  );
}
