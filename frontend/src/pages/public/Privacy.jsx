// GDPR privacy notice, version 1.0 (features/public/legal/privacy.js).
import React from "react";
import { useT } from "@/lib/i18n";
import LegalDoc from "@/features/public/LegalDoc";
import PrivacyRequestForm from "@/features/compliance/PrivacyRequestForm";
import privacy, { PRIVACY_DATE, PRIVACY_VERSION } from "@/features/public/legal/privacy";

export default function Privacy() {
  const { t, locale } = useT();
  return (
    <LegalDoc
      title={t("pub.privacy.title")}
      lead={t("pub.privacy.lead")}
      version={PRIVACY_VERSION}
      date={PRIVACY_DATE}
      sections={privacy[locale] || privacy.en}
    >
      <PrivacyRequestForm />
    </LegalDoc>
  );
}
