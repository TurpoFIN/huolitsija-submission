import React from "react";
import { useSearchParams } from "react-router-dom";
import ShipmentMap from "@/features/map/ShipmentMap";
import { useT } from "@/lib/i18n";

export default function MapPage() {
  const { t } = useT();
  const [params] = useSearchParams();
  return (
    <div>
      <h1 className="sr-only">{t("nav.map")}</h1>
      <ShipmentMap scope="customer" height="calc(100dvh - 104px)" initialSelectedId={params.get("select") || undefined} initialView={params.get("view") || undefined} />
    </div>
  );
}
