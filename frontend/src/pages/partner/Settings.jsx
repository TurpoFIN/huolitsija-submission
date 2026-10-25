import React from "react";
import { PageHeader } from "@/components/app/layout";
import { ProfileSettings } from "@/features/ProfileSettings";
import { useT } from "@/lib/i18n";

/** Partner profile, language, theme and password (no e-mail notification switch: partners get job e-mails by role). */
export default function PartnerSettings() {
  const { t } = useT();
  return (
    <div className="max-w-[1024px] px-1 sm:px-4">
      <PageHeader title={t("settings.title")} />
      <ProfileSettings showNotifications={false} />
    </div>
  );
}
