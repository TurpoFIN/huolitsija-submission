import React from "react";
import { PublicLayout } from "@/features/public/PublicLayout";
import { CompanyIdentity } from "@/features/public/CompanyIdentity";
import { useT } from "@/lib/i18n";
export default function CompanyInfo() {
  const { locale } = useT();
  return <PublicLayout><article className="mx-auto max-w-[72ch] px-4 py-8 sm:px-6"><h1 className="mb-6 text-2xl">{locale === "fi" ? "Yritystiedot ja yhteystiedot" : "Company details and contact"}</h1><CompanyIdentity /></article></PublicLayout>;
}
