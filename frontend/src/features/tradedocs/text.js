export const text = {
  title: ['Laaditut asiakirjat', 'Generated documents'], empty: ['Ei laadittuja asiakirjoja.', 'No generated documents.'],
  create: ['Laadi asiakirja', 'Generate document'], kind: ['Asiakirjalaji', 'Document type'], draft: ['Luonnos', 'Draft'],
  issued: ['Laadittu', 'Generated'], superseded: ['Korvattu', 'Superseded'], cancelled: ['Peruttu', 'Cancelled'],
  amend: ['Korjaa', 'Amend'], history: ['Versiot ja lähetykset', 'Versions and deliveries'], send: ['Lähetä osapuolille', 'Send to parties'],
  reason: ['Korjauksen syy', 'Reason for amendment'], field: ['Korjattava tieto', 'Field to amend'], value: ['Uusi arvo', 'New value'],
  current: ['Nykyinen arvo', 'Current value'], save: ['Tallenna uusi versio', 'Save new version'], loading: ['Ladataan…', 'Loading…'],
  saved: ['Uusi versio tallennettu.', 'New version saved.'], generated: ['Asiakirja laadittu.', 'Document generated.'],
  missing: ['Tieto puuttuu', 'Missing data'], version: ['Versio', 'Version'], close: ['Sulje', 'Close'],
  sent: ['Lähetetty palveluntarjoajalle', 'Submitted to mail provider'], failed: ['Lähetys epäonnistui', 'Delivery failed'],
  logged: ['Vain lokissa — ei lähetetty', 'Logged only — not sent'], queued: ['Jonossa', 'Queued'],
  notSent: ['Ei lähetyksiä.', 'No deliveries.'], pending: ['Odottaa hyväksyntää', 'Awaiting approval'],
  incomplete: ['Vastaanottajatietoja puuttuu', 'Recipient details missing'],
  historyNote: ['Aiemmat PDF-versiot säilyvät muuttumattomina.', 'Previous PDF versions remain unchanged.'],
  generateNote: ['Alkuperä- ja EUR.1-asiakirjat ovat hakemuksia. Viranomainen myöntää todistukset.', 'Origin and EUR.1 documents are applications. Certificates are issued by the authority.'],
  pdf: ['PDF suomeksi', 'PDF in Finnish'], pdfEn: ['PDF englanniksi', 'PDF in English'],
  promote: ['Laadi lopullinen', 'Generate final'], retry: ['Yritä uudelleen', 'Retry'],
};
export const fields = [
  ['consignee.name', 'Vastaanottajan nimi', 'Consignee name'], ['consignee.address.street', 'Vastaanottajan katuosoite', 'Consignee street'],
  ['consignee.address.city', 'Vastaanottajan kaupunki', 'Consignee city'], ['consignee.email', 'Vastaanottajan sähköposti', 'Consignee email'],
  ['shipper.name', 'Lähettäjän nimi', 'Shipper name'], ['shipper.address.street', 'Lähettäjän katuosoite', 'Shipper street'],
  ['shipper.email', 'Lähettäjän sähköposti', 'Shipper email'], ['refs.customer', 'Asiakkaan viite', 'Customer reference'],
  ['refs.booking', 'Varausnumero', 'Booking reference'], ['refs.mbl', 'Master B/L -numero', 'Master B/L reference'],
  ['refs.awb_master', 'Master AWB -numero', 'Master AWB reference'], ['main_leg.vessel', 'Alus', 'Vessel'],
  ['main_leg.voyage', 'Matka', 'Voyage'], ['main_leg.flight', 'Lento', 'Flight'],
];
export function deliveryState(rows = []) {
  const latest = new Map();
  rows.forEach(r => latest.set(r.to || r.role, r));
  if (!latest.size) return 'notSent';
  if ([...latest.values()].some(r => r.status === 'failed')) return 'failed';
  if ([...latest.values()].some(r => r.status === 'logged')) return 'logged';
  if ([...latest.values()].every(r => r.status === 'sent')) return 'sent';
  return 'queued';
}
