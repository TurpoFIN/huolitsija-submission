import React from "react";
import { PageHeader } from "@/components/app/layout";
import { ProfileSettings } from "@/features/ProfileSettings";
import PrivacyPanel from "@/features/compliance/PrivacyPanel";
import { useT } from "@/lib/i18n";

export default function Settings() {
  const { t } = useT();
  return (
    <div className="max-w-[1024px] px-1 sm:px-4">
      <PageHeader title={t("settings.title")} />
      <ProfileSettings />
      <PrivacyPanel />
    </div>
  );
}
