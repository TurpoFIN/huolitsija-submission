// Vakuutus panel on the ops shipment page: the shipment's policies and a quote + bind form.
// Mounted with one import + one line in pages/ops/ShipmentDetail.jsx.
import React, { useMemo } from "react";
import { useParams } from "react-router-dom";
import { Section } from "@/components/app/layout";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { ComLoadable } from "./common";
import { InsuranceQuoteForm, PolicyList } from "./InsuranceQuote";
import { listOf, policy as normPolicy, providers as normProviders } from "./shapes";

const goodsValue = (s) => {
  if (!s) return null;
  const c = s.cargo || {};
  return s.insured_value_cents ?? c.value_cents ?? s.goods_value_cents ?? c.goods_value_cents ?? s.invoice_value_cents ?? s.customs_value_cents ?? null;
};

export default function InsurancePanel() {
  const { id } = useParams();
  const { t } = useT();
  const { can } = useAuth();
  const pols = useApi(id ? `/insurance/policies?shipment_id=${encodeURIComponent(id)}` : null);
  const prov = useApi(id ? "/insurance/providers" : null);
  const ship = useApi(id ? `/shipments/${id}` : null);
  const policies = useMemo(() => listOf(pols.data, "policies").map(normPolicy), [pols.data]);
  const providers = useMemo(() => normProviders(prov.data), [prov.data]);
  const s = ship.data?.shipment || ship.data;
  const bound = policies.some((p) => ["bound", "active"].includes(p.status));
  if (!id) return null;
  return (
    <div className="mt-6 max-w-[1280px]">
      <Section title={t("com.ins.panel")}>
        <ComLoadable q={pols} perm="quotes.write">
          {() => (
            <div className="flex flex-col gap-4">
              <PolicyList items={policies} />
              {can("quotes.write") && !bound && !["delivered", "cancelled"].includes(s?.status) && (
                <InsuranceQuoteForm key={goodsValue(s) ?? "x"} shipmentId={id} defaultValueCents={goodsValue(s)} providers={providers} onBound={() => pols.reload()} />
              )}
            </div>
          )}
        </ComLoadable>
      </Section>
    </div>
  );
}
