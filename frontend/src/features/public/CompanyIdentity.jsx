import React, { createContext, useContext } from "react";
import { Link } from "react-router-dom";
import { useApi } from "@/lib/api";
import { useT } from "@/lib/i18n";

const LegalContext = createContext(null);
export function LegalConfigProvider({ children }) {
  const result = useApi("/legal/config");
  return <LegalContext.Provider value={result}>{children}</LegalContext.Provider>;
}
export function CompanyIdentity({ compact = false }) {
  const { locale } = useT();
  const fi = locale === "fi";
  const result = useContext(LegalContext);
  const c = result?.data?.company;
  const missing = fi ? "Ei vielä ilmoitettu" : "Not yet provided";
  if (!c) return <p className="text-sm text-ink-secondary" role="status">{result?.error ? (fi ? "Yritystietoja ei voitu ladata. Yritä päivittää sivu." : "Company details could not be loaded. Try refreshing the page.") : (fi ? "Ladataan yritystietoja…" : "Loading company details…")}</p>;
  if (compact) return <address className="not-italic text-ink-secondary"><Link to="/company" className="underline underline-offset-4">{c.name}</Link><span className="block">{fi ? "Y-tunnus" : "Business ID"}: {c.business_id || missing}</span><span className="block">{c.address || (fi ? "Osoitetta ei vielä ilmoitettu" : "Address not yet provided")}</span></address>;
  const rows = [[fi ? "Yritys" : "Legal name", c.name], [fi ? "Y-tunnus" : "Business ID", c.business_id], [fi ? "ALV-tunniste" : "VAT ID", c.vat_id], ["EORI", c.eori], [fi ? "Osoite" : "Address", c.address], [fi ? "Kotipaikka" : "Registered office", c.registered_office], [fi ? "Sähköposti" : "Email", c.email], [fi ? "Tietosuoja" : "Privacy contact", c.privacy_email], [fi ? "Puhelin (valinnainen)" : "Phone (optional)", c.phone]];
  return <section id="contact" className="scroll-mt-20">
    <p className="mb-4 text-sm text-ink-secondary">{c.registered ? (fi ? "Rekisteröity yritys (palveluntarjoajan ilmoitus)." : "Registered company (as declared by the operator).") : (fi ? "Perusteilla. Rekisteröintiä ei ole vielä vahvistettu. Ennen rekisteröintiä tehdyn sopimuksen vastuullinen osapuoli on yksilöitävä erikseen; tuleva yhtiö ei vielä ole sopimusosapuoli." : "In formation. Registration has not yet been confirmed. Any agreement before registration must separately identify its responsible contracting party; the future company is not yet the contracting party.")}</p>
    <dl className="divide-y divide-[color:var(--border-subtle)]">{rows.map(([label,value]) => <div key={label} className="grid gap-1 py-3 sm:grid-cols-[160px_minmax(0,1fr)]"><dt className="text-ink-secondary">{label}</dt><dd className="break-words">{value || missing}</dd></div>)}</dl>
  </section>;
}
