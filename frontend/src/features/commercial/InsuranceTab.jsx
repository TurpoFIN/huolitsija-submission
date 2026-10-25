// Vakuutukset: which insurer answers (live, or live-ready waiting for the insurer contract), policies bound, quote + bind.
import React, { useMemo, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { Section } from "@/components/app/layout";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { num } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Field, NativeSelect } from "@/features/quotes/ops/common";
import { ComLoadable, NeedsLine } from "./common";
import { InsuranceQuoteForm, PolicyList, ProviderMark } from "./InsuranceQuote";
import { listOf, policy as normPolicy, providers as normProviders } from "./shapes";

export default function InsuranceTab() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const canWrite = can("quotes.write");
  const prov = useApi("/insurance/providers");
  const pols = useApi("/insurance/policies", { pollMs: 60000 });
  const ships = useApi(canWrite ? "/shipments?limit=100" : null);
  const [sid, setSid] = useState(null);
  const providers = useMemo(() => normProviders(prov.data), [prov.data]);
  const policies = useMemo(() => listOf(pols.data, "policies").map(normPolicy), [pols.data]);
  const shipOptions = useMemo(() => listOf(ships.data, "shipments").filter((s) => !["delivered", "cancelled"].includes(s.status))
    .map((s) => ({ value: s.id, label: [s.reference, s.customer_name, `${s.origin?.unlocode || s.origin?.name || ""} → ${s.destination?.unlocode || s.destination?.name || ""}`].filter(Boolean).join(" · ") })), [ships.data]);
  const active = providers.find((p) => p.active) || null;

  return (
    <div>
      <NeedsLine items={[
        active && active.status !== "live" && { tone: "warning", icon: ShieldAlert,
          text: t("com.ins.blocked_warning", { what: (locale === "fi" ? active.blocked_on_fi : active.blocked_on) || active.name }) },
      ]} quiet={prov.data && active?.status === "live" ? t("com.ins.live_line", { name: active.name, n: num(policies.length, locale) }) : null} />

      <Section title={t("com.ins.providers")}>
        <ComLoadable q={prov} perm="quotes.write" isEmpty={() => providers.length === 0} empty={<p className="text-sm text-ink-tertiary">{t("com.ins.no_providers")}</p>}>
          {() => (
            <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] border-y">
              {providers.map((p) => (
                <li key={p.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2 text-sm">
                  <ProviderMark status={p.status} name={locale === "fi" && p.label_fi ? p.label_fi : p.name} />
                  {p.active && <span className="text-xs font-medium">{t("com.ins.in_use")}</span>}
                  {(p.blocked_on_fi || p.blocked_on) && <span className="text-xs text-ink-tertiary">{locale === "fi" ? p.blocked_on_fi || p.blocked_on : p.blocked_on || p.blocked_on_fi}</span>}
                  {p.env && !p.active && <span className="ml-auto font-mono text-[11px] text-ink-tertiary">{p.env}</span>}
                </li>
              ))}
            </ul>
          )}
        </ComLoadable>
      </Section>

      {canWrite && (
        <Section title={t("com.ins.quote_title")}>
          <InsuranceQuoteForm shipmentId={sid} providers={providers} onBound={() => pols.reload()}
            shipmentPicker={
              <Field label={t("com.ins.shipment")} htmlFor="ins-ship">
                <NativeSelect id="ins-ship" value={sid} onChange={setSid} placeholder={ships.loading ? t("state.loading") : t("com.choose")} options={shipOptions} />
              </Field>
            } />
        </Section>
      )}

      <Section title={t("com.ins.policies")}>
        <ComLoadable q={pols} perm="quotes.write">{() => <PolicyList items={policies} showShipment />}</ComLoadable>
      </Section>
    </div>
  );
}
