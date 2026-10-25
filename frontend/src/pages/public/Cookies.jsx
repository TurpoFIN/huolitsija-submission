import React from "react";
import { useT } from "@/lib/i18n";
import LegalDoc from "@/features/public/LegalDoc";
import cookies from "@/features/public/legal/cookies";
export default function Cookies() {
  const { locale } = useT();
  const fi = locale === "fi";
  return <LegalDoc title={fi ? "Evästeet ja selaintallennus" : "Cookies and browser storage"} lead={fi ? "Mitä tallennamme laitteellesi ja miten poistat tiedot." : "What we store on your device and how to remove it."} version="1.1" date="2026-10-24" sections={cookies[locale] || cookies.en} />;
}
