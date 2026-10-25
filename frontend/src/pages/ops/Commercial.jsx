// Ops Myynti / Commercial hub. Each tab opens with what needs a person and what the agents are doing.
import React, { Suspense, lazy } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { PageHeader } from "@/components/app/layout";
import { LoadingRows } from "@/components/app/states";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { COMMERCIAL_TABS } from "@/features/commercial/common";

const Pipeline = lazy(() => import("@/features/commercial/PipelineTab"));
const Tenders = lazy(() => import("@/features/commercial/TendersTab"));
const TenderDetail = lazy(() => import("@/features/commercial/TenderDetail"));
const Tariffs = lazy(() => import("@/features/commercial/TariffsTab"));
const TariffDetail = lazy(() => import("@/features/commercial/TariffsTab").then((m) => ({ default: m.TariffDetail })));
const MarginRules = lazy(() => import("@/features/commercial/MarginRulesTab"));
const Credit = lazy(() => import("@/features/commercial/CreditTab"));
const Insurance = lazy(() => import("@/features/commercial/InsuranceTab"));


export default function Commercial() {
  const { t } = useT();
  return (
    <div className="max-w-[1280px]">
      <PageHeader title={t("nav.commercial")} />
      <nav aria-label={t("com.tabs")} className="-mx-1 mb-4 overflow-x-auto no-scrollbar border-b border-[color:var(--border-subtle)]">
        <ul className="flex min-w-max gap-1 px-1">
          {COMMERCIAL_TABS.map((k) => (
            <li key={k}>
              <NavLink to={`/ops/commercial/${k}`}
                className={({ isActive }) => cn("-mb-px inline-flex h-9 items-center whitespace-nowrap border-b-2 px-2.5 text-sm",
                  isActive ? "border-[color:var(--text-primary)] font-medium text-foreground" : "border-transparent text-ink-secondary hover:text-foreground")}>
                {t(`com.tab.${k}`)}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <Suspense fallback={<LoadingRows rows={6} />}>
        <Routes>
          <Route index element={<Navigate to="pipeline" replace />} />
          <Route path="pipeline" element={<Pipeline />} />
          <Route path="tenders" element={<Tenders />} />
          <Route path="tenders/:id" element={<TenderDetail />} />
          <Route path="tariffs" element={<Tariffs />} />
          <Route path="tariffs/:id" element={<TariffDetail />} />
          <Route path="margins" element={<MarginRules />} />
          <Route path="credit" element={<Credit />} />
          <Route path="insurance" element={<Insurance />} />
          <Route path="*" element={<Navigate to="pipeline" replace />} />
        </Routes>
      </Suspense>
    </div>
  );
}
