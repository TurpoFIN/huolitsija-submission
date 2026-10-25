import React from "react";
import { useT } from "@/lib/i18n";
import LegalDoc from "@/features/public/LegalDoc";
import dpa from "@/features/public/legal/dpa";
export default function Dpa() {
  const { locale } = useT();
  const fi = locale === "fi";
  return <LegalDoc title={fi ? "Henkilötietojen käsittelysopimus" : "Data processing agreement"} lead={fi ? "Sopimusehdot asiakkaan lukuun tehtävään käsittelyyn. Käsittelyliite sovitaan ennen palvelun aloittamista." : "Terms for processing on the customer’s behalf. Agree the processing annex before starting the service."} version="1.0" date="2026-10-24" sections={dpa[locale] || dpa.en} />;
}
