// Ops: Finance (Talous) route group. One title, the sub-navigation between the nine finance screens, then the screen.
import React, { Suspense } from "react";
import { Outlet } from "react-router-dom";
import { LoadingRows } from "@/components/app/states";
import { useT } from "@/lib/i18n";
import { FinanceTabs } from "@/features/finance/kit";

export default function FinanceLayout() {
  const { t } = useT();
  return (
    <div className="min-w-0">
      <h1 className="mb-3 text-xl leading-7">{t("nav.finance")}</h1>
      <FinanceTabs />
      <Suspense fallback={<LoadingRows rows={8} />}>
        <Outlet />
      </Suspense>
    </div>
  );
}
