// Presentation projection for system-generated data. User-authored text is preserved.
// New agents persist explicit *_i18n variants; exact legacy strings are a migration bridge.
import storedFi from '@/i18n/storedText.fi.json';
import { customsTextFi } from '@/i18n/customsChecks.fi';
import { backendTextFi } from '@/i18n/backendText.fi';

export function currentLocale() {
  try { return localStorage.getItem('huo.locale') === 'en' ? 'en' : 'fi'; } catch { return 'fi'; }
}

export function displayText(text, locale, variants) {
  if (variants?.[locale] !== undefined) return variants[locale];
  if (typeof text !== 'string' || locale !== 'fi') return text;
  if (storedFi[text]) return storedFi[text];
  const customs = customsTextFi(text);
  if (customs) return customs;
  const backend = backendTextFi(text, (part) => displayText(part, locale));
  if (backend) return backend;
  if(text.startsWith('Two pallets damaged at Travemünde terminal (forklift).'))return storedFi['Two pallets damaged at Travemünde terminal (forklift). Re-wrapped and photographed; trailer re-booked on the next Finnlines sailing.'];
  if(text.startsWith('AI is not configured:') || text.startsWith('Needs a person: No AI provider'))return 'AI-yhteyttä ei ole otettu käyttöön. Käsittele asia käsin tai pyydä ylläpitäjää ottamaan yhteys käyttöön.';
  let m;
  if ((m = text.match(/^Reply to (.+): (.+)$/))) return `Vastaus yritykselle ${m[1]}: ${m[2]}`;
  if ((m = text.match(/^KYC review: (.+)$/))) return `Asiakastuntemuksen tarkistus: ${m[1]}`;
  if ((m = text.match(/^Claim (.+): (pay|approve|reject)$/))) return `Reklamaatio ${m[1]}: ${{pay:'maksu',approve:'hyväksyntä',reject:'hylkäys'}[m[2]]}`;
  if ((m = text.match(/^Customs hold: (.+)$/))) return `Tullipysäytys: ${displayText(m[1],locale)}`;
  if ((m = text.match(/^(.+?): (Two pallets|Truck queued|Container rolled)([\s\S]*)$/))) return `${m[1]}: ${displayText(m[2]+m[3],locale)}`;
  if ((m = text.match(/^([A-Z]{3}): freight and insurance to the EU border are not known; enter them under valuation$/))) return `${m[1]}: rahti ja vakuutus EU:n rajalle puuttuvat. Syötä ne tullausarvon tietoihin.`;
  if ((m = text.match(/^Accepted, MRN (\S+)( \(demo\))?$/))) return `Hyväksytty, MRN ${m[1]}${m[2]?' (esimerkki)':''}`;
  if ((m = text.match(/^(Suora|Epäsuora|Välillinen) edustus, voimassa (\d{4}-\d{2}-\d{2}) asti$/))) return `${m[1]} edustus, voimassa ${new Date(m[2]+'T12:00:00Z').toLocaleDateString('fi-FI')} asti`;
  if ((m = text.match(/^(direct|indirect) representation, valid to (\d{4}-\d{2}-\d{2})$/))) return `${m[1]==='direct'?'Suora':'Välillinen'} edustus, voimassa ${new Date(m[2]+'T12:00:00Z').toLocaleDateString('fi-FI',{timeZone:'Europe/Helsinki'})} asti`;
  if ((m = text.match(/^(\d+) beneficial owner\(s\) declared$/))) return `${m[1]} tosiasiallista edunsaajaa ilmoitettu`;
  if ((m = text.match(/^Freight and insurance up to the EU border are added([\s\S]*)$/))) return 'Rahti ja vakuutus EU:n rajalle lisätään kauppahintaan (IA 137 art.). Tiedot hyväksytyn tarjouksen riveiltä.';
  if ((m = text.match(/^Edited by (.+) on (\d{4}-\d{2}-\d{2})$/))) return `Muokkaaja ${m[1]} · ${new Date(m[2]+'T12:00:00Z').toLocaleDateString('fi-FI')}`;
  if ((m = text.match(/^Filed in Tulli's Customs Clearance Service( \(demo\))?$/))) return `Lähetetty Tullin tulliselvityspalvelussa${m[1]?' (esimerkki)':''}`;
  if ((m = text.match(/^Released by Tulli, MRN (.+)$/))) return `Tulli luovutti tavaran, MRN ${m[1]}`;
  if ((m = text.match(/^New invoice: (.+)$/))) return `Uusi lasku: ${m[1]}`;
  return text;
}

export function localizePayload(value, locale=currentLocale(), field) {
  if (Array.isArray(value)) return value.map(v=>typeof v === 'string' ? (ARRAY_FIELDS.has(field) ? displayText(v,locale) : v) : localizePayload(v,locale));
  if (!value || typeof value !== 'object') return value;
  const out={};
  for (const [key,v] of Object.entries(value)) {
    if (key.endsWith('_i18n')) { out[key]=v; continue; }
    const own=DISPLAY_FIELDS.has(key) || ARRAY_FIELDS.has(key) ? value[key+'_i18n']?.[locale] : undefined;
    if (own !== undefined) out[key]=localizePayload(own,locale,key);
    else if (typeof v==='object') out[key]=localizePayload(v,locale,key);
    else if (typeof v==='string' && DISPLAY_FIELDS.has(key)) out[key]=displayText(v,locale);
    else out[key]=v;
  }
  return out;
}
const ARRAY_FIELDS=new Set(['recommended_actions','facts_used','notes']);
const DISPLAY_FIELDS=new Set(['title','description','summary','reason','note','notes','body','detail','source','name','method_label','rule','service_name','exception_note','impact','label','message','ai_note','cargo_description','note_fi','direction_label','by','vat_reason','rationale','summary_fi','preference_note','services_note','path_note','dg_note','opening_hours','instructions']);
