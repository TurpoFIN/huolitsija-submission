import { domainErrors } from "@/features/domainErrors";
const en = {
  "domain.legal_version": "Version {v} · Effective {date}",
  "domain.select": "Select", "domain.all_in": "The verified carrier offer includes all mandatory charges for the RFQ basis and equipment.",
  "domain.autonomy": "Autonomous operations", "domain.autopilot": "Autopilot", "domain.network": "Sourcing Autopilot",
  "domain.on": "On", "domain.off": "Off", "domain.enable": "Enable", "domain.disable": "Disable", "domain.saved": "Saved",
  "domain.confirm_title": "Enable {name}", "domain.confirm": "Type enable to confirm. Autonomous actions are logged. Sent emails and customs filings cannot be recalled.",
  "domain.autopilot_hint": "Send customer replies, approve quotes within policy, file validated declarations through the configured customs channel, resolve exceptions and issue invoices.",
  "domain.network_hint": "Finds carriers, agents and hauliers in public registers (YTJ, Wikidata, carrier directory), e-mails them quote requests, reads their replies, negotiates within the limits below and loads accepted rates into the rate table. Separate from Autopilot. B2B only, opt-outs honoured, daily e-mail limit.",
  "domain.policy": "Autonomy policy", "domain.partner_target_margin_pct": "Partner target margin (%)", "domain.partner_max_price_cents": "Maximum purchase rate (€)",
  "domain.partner_sell_price_cents": "Planned selling rate (€ per basis)", "domain.autopilot_quote_max_cents": "Maximum autonomous quote (€)",
  "domain.autopilot_min_margin_pct": "Minimum quote margin (%)", "domain.lanes": "Approved lanes", "domain.lanes_hint": "One per line, for example road:FI>SE. An empty list permits no outreach.",
  "domain.bank": "Seller and bank transfer", "domain.seller_business_id": "Seller business ID", "domain.seller_vat_id": "Seller VAT ID", "domain.seller_iban": "Seller IBAN", "domain.seller_bic": "Seller BIC",
  "domain.setup": "Set up bank transfer: enter the seller's IBAN, BIC, business ID and VAT ID below. Only enter your registered business details.",
  "domain.outreach": "Partner outreach", "domain.run": "Find partners now", "domain.no_outreach": "No partner outreach yet. Enable Partner Network and configure approved lanes.",
  "domain.carrier": "Carrier", "domain.lane": "Lane", "domain.contact": "Published contact", "domain.state": "State", "domain.log": "Outreach history",
  "domain.show_tests": "Show test tenants", "domain.env": "Example environment · shipment and company data are illustrative; integrations follow their configured mode.",
  "domain.active": "Autopilot on", "domain.actions": "Autonomous actions", "domain.actions_log": "Action log", "domain.undo": "Reverse action", "domain.irreversible": "Cannot be recalled",
  "domain.decision": "Finnish Customs decision", "domain.decision_pending": "Duty and import VAT remain estimates. Record the decided amounts from the customs decision before invoicing customs charges.",
  "domain.decision_date": "Decision date", "domain.duty": "Decided duty (€)", "domain.vat": "Decided import VAT (€)", "domain.paid_by": "Customs debt paid by", "domain.forwarder": "Forwarder", "domain.importer": "Importer",
  "domain.record_decision": "Record decision", "domain.recorded": "Recorded decision", "domain.item": "Goods item", "domain.decision_help": "Copy each amount from the decision, including explicit zeroes. Estimates are not prefilled.",
  "domain.risk": "Trade-defence assessment", "domain.risk_hint": "For each flagged item enter a TARIC additional code, or explain the evidence clearing the anti-dumping risk.", "domain.code": "TARIC additional code", "domain.reason": "Reason clearing the risk", "domain.save_risk": "Save assessment",
  "domain.accept": "Accept rate", "domain.revoke": "Revoke rate", "domain.offer": "Record carrier offer", "domain.cost": "Purchase rate (€)", "domain.days": "Transit days", "domain.valid_to": "Valid until",
};
const fi = {
  "domain.legal_version": "Versio {v} · Voimassa {date} alkaen",
  "domain.select": "Valitse", "domain.all_in": "Tarkistettu kumppanitarjous sisältää kaikki pakolliset kulut pyynnön hinnoitteluyksikölle ja kalustolle.",
  "domain.autonomy": "Autonomiset toiminnot", "domain.autopilot": "Autopilot", "domain.network": "Hankinta-autopilotti",
  "domain.on": "Käytössä", "domain.off": "Pois käytöstä", "domain.enable": "Ota käyttöön", "domain.disable": "Poista käytöstä", "domain.saved": "Tallennettu",
  "domain.confirm_title": "Ota {name} käyttöön", "domain.confirm": "Vahvista kirjoittamalla enable. Autonomiset toimet kirjataan. Lähetettyjä sähköposteja ja tullauksia ei voi perua tästä palvelusta.",
  "domain.autopilot_hint": "Lähettää asiakasvastauksia, hyväksyy tarjoukset toimintaperiaatteiden rajoissa, välittää tarkistetut tulli-ilmoitukset määritettyyn kanavaan, ratkaisee poikkeamia ja laskuttaa.",
  "domain.network_hint": "Etsii kuljetusliikkeitä, agentteja ja rahdinkuljettajia julkisista rekistereistä (YTJ, Wikidata, kuljetusliikehakemisto), lähettää tarjouspyynnöt, lukee vastaukset, neuvottelee alla olevissa rajoissa ja vie hyväksytyt hinnat hinnastoon. Erillään Autopilotista. Vain yrityksille, poistopyynnöt toteutetaan, päivittäinen lähetysraja.",
  "domain.policy": "Automaation rajat", "domain.partner_target_margin_pct": "Kumppanihinnan tavoitekate (%)", "domain.partner_max_price_cents": "Ostohinnan enimmäismäärä (€)",
  "domain.partner_sell_price_cents": "Suunniteltu myyntihinta (€ / hinnoitteluyksikkö)", "domain.autopilot_quote_max_cents": "Autonomisen tarjouksen enimmäismäärä (€)",
  "domain.autopilot_min_margin_pct": "Tarjouksen vähimmäiskate (%)", "domain.lanes": "Sallitut reitit", "domain.lanes_hint": "Yksi riville, esimerkiksi road:FI>SE. Tyhjä lista ei salli yhteydenottoja.",
  "domain.bank": "Myyjä ja tilisiirto", "domain.seller_business_id": "Myyjän Y-tunnus", "domain.seller_vat_id": "Myyjän ALV-tunnus", "domain.seller_iban": "Myyjän IBAN", "domain.seller_bic": "Myyjän BIC",
  "domain.setup": "Ota tilisiirto käyttöön: täytä myyjän IBAN, BIC, Y-tunnus ja ALV-tunnus alle. Käytä rekisteröidyn yrityksesi tietoja.",
  "domain.outreach": "Kumppaniyhteydenotot", "domain.run": "Etsi kumppaneita nyt", "domain.no_outreach": "Ei kumppaniyhteydenottoja. Ota kumppaniverkosto käyttöön ja määritä sallitut reitit.",
  "domain.carrier": "Kuljetusliike", "domain.lane": "Reitti", "domain.contact": "Julkinen yhteystieto", "domain.state": "Tila", "domain.log": "Yhteydenottohistoria",
  "domain.show_tests": "Näytä testiyritykset", "domain.env": "Esittely-ympäristö · lähetys- ja yritystiedot ovat esimerkkejä; integraatiot noudattavat määritettyä toimintatilaa.",
  "domain.active": "Autopilot käytössä", "domain.actions": "Autonomiset toimet", "domain.actions_log": "Toimintoloki", "domain.undo": "Kumoa toimi", "domain.irreversible": "Ei peruttavissa",
  "domain.decision": "Tullin päätös", "domain.decision_pending": "Tulli ja tuonnin ALV ovat arvioita. Kirjaa tullauspäätöksen mukaiset määrät ennen tullierien laskutusta.",
  "domain.decision_date": "Päätöspäivä", "domain.duty": "Päätetty tulli (€)", "domain.vat": "Päätetty tuonnin ALV (€)", "domain.paid_by": "Tullivelan maksaja", "domain.forwarder": "Huolitsija", "domain.importer": "Maahantuoja",
  "domain.record_decision": "Kirjaa päätös", "domain.recorded": "Kirjattu päätös", "domain.item": "Tavaraerä", "domain.decision_help": "Kopioi jokainen summa päätöksestä, myös nollat. Arvioita ei esitäytetä.",
  "domain.risk": "Polkumyyntiriskin arvio", "domain.risk_hint": "Kirjaa merkitylle erälle TARIC-lisäkoodi tai perustele, miksi polkumyyntitullia ei sovelleta.", "domain.code": "TARIC-lisäkoodi", "domain.reason": "Riskin poistamisen peruste", "domain.save_risk": "Tallenna arvio",
  "domain.accept": "Hyväksy hinta", "domain.revoke": "Poista hinta käytöstä", "domain.offer": "Kirjaa kumppanin tarjous", "domain.cost": "Ostohinta (€)", "domain.days": "Kuljetusaika (pv)", "domain.valid_to": "Voimassa asti",
};
const states = {discovered:["Discovered","Löydetty"],contact_unavailable:["Contact unavailable","Yhteystieto puuttuu"],logged:["Logged, not sent","Kirjattu, ei lähetetty"],awaiting_reply:["Awaiting reply","Odottaa vastausta"],email_failed:["Email failed","Lähetys epäonnistui"],pending:["Rate pending approval","Hinta odottaa hyväksyntää"],accepted:["Rate accepted","Hinta hyväksytty"],rejected:["Rate rejected","Hinta hylätty"],revoked:["Rate revoked","Hinta poistettu käytöstä"],outside_policy:["Outside policy","Ylittää rajat"],needs_person:["Needs a person","Vaatii käsittelyä"],rfq:["Quote request","Tarjouspyyntö"],counteroffer:["Counteroffer","Vastatarjous"],offer:["Carrier offer","Kumppanin tarjous"],ran:["Sourcing run finished","Haku päättynyt"],needs_policy:["Configure approved lanes","Määritä sallitut reitit"]};
Object.entries(states).forEach(([k,[e,f]])=>{en[`domain.status.${k}`]=e;fi[`domain.status.${k}`]=f;});
const actions={customer_reply_sent:["Customer reply sent","Asiakasvastaus lähetetty"],quote_approved:["Quote approved","Tarjous hyväksytty"],customs_approved:["Customs filing approved","Tullaus hyväksytty"],invoice_issued:["Invoice issued","Lasku lähetetty"],exception_opened:["Exception opened","Poikkeama avattu"],exception_resolved:["Exception resolved","Poikkeama ratkaistu"],partner_rfq:["Partner quote requested","Kumppanitarjous pyydetty"],partner_counteroffer:["Partner counteroffer sent","Kumppanille lähetetty vastatarjous"],partner_rate_pending:["Partner rate awaits approval","Kumppanihinta odottaa hyväksyntää"],partner_rate_accepted:["Partner rate accepted","Kumppanihinta hyväksytty"],ai_message_approved:["Customer reply approved","Asiakasvastaus hyväksytty"],quote_threshold_approved:["Quote approved within policy","Tarjous hyväksytty rajoissa"],customs_filing_approved:["Customs filing approved","Tullaus hyväksytty"]};
Object.entries(actions).forEach(([k,[e,f]])=>{en[`domain.action.${k}`]=e;fi[`domain.action.${k}`]=f;});
domainErrors.forEach(([e,f],i)=>{en[`domain.error.${i}`]=e;fi[`domain.error.${i}`]=f;});
export default {en,fi};
