// General terms, version 1.0 (features/public/legal/terms.js); incorporates NSAB 2015 (PSYM 2015).
import React from "react";
import { useT } from "@/lib/i18n";
import LegalDoc from "@/features/public/LegalDoc";
import terms, { TERMS_DATE, TERMS_VERSION } from "@/features/public/legal/terms";

export default function Terms() {
  const { t, locale } = useT();
  return (
    <LegalDoc
      title={t("pub.terms.title")}
      lead={t("pub.terms.lead")}
      version={TERMS_VERSION}
      date={TERMS_DATE}
      sections={terms[locale] || terms.en}
    />
  );
}
