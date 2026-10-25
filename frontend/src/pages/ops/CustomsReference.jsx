// Customs reference pages under the customs hub: Tullitariffi (TARIC lookup), licence and certificate register,
// customs controls (who may file, four-eyes thresholds, KPIs). One page component, the section comes from the path.
import React from "react";
import { useLocation } from "react-router-dom";
import { PageHeader } from "@/components/app/layout";
import { useT } from "@/lib/i18n";
import { CustomsHubTabs, hubKeyFor } from "@/features/customs/extCommon";
import TaricLookup from "@/features/customs/TaricLookup";

import LicenceRegister from "@/features/customs/TaricLicences";
import ControlsSettings from "@/features/customs/ControlsSettings";

import TaricBti from "@/features/customs/TaricBti";

const TITLES = { taric: "cx.taric.title", licences: "cx.lic.title", controls: "cx.ctl.title" };

export default function CustomsReference() {
  const { t } = useT();
  const loc = useLocation();
  const section = hubKeyFor(loc.pathname);
  return (
    <div className="min-w-0">
      <PageHeader crumbs={[{ label: t("nav.customs"), to: "/ops/customs" }, { label: t(`cx.hub.${section}`) }]} title={t(TITLES[section] || "cx.taric.title")} />
      <CustomsHubTabs active={section} />
      {section === "taric" && <><TaricLookup /><TaricBti code={new URLSearchParams(loc.search).get("code")} /></>}
      {section === "licences" && <LicenceRegister />}
      {section === "controls" && <ControlsSettings />}
    </div>
  );
}
