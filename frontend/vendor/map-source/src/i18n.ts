import type { LegMode, Locale, Mode, PositionSource, Status } from './types';

const en = {
  mode: { sea: 'Sea', road: 'Road', air: 'Air', rail: 'Rail', multimodal: 'Multimodal' } as Record<Mode, string>,
  status: {
    booked: 'Booked',
    picked_up: 'Picked up',
    in_transit: 'In transit',
    at_hub: 'At hub',
    customs_hold: 'Customs hold',
    out_for_delivery: 'Out for delivery',
    delivered: 'Delivered',
    exception: 'Exception',
  } as Record<Status, string>,
  source: {
    adsb: 'ADS-B flight',
    ais: 'AIS',
    gps: 'GPS',
    carrier_api: 'Carrier feed',
    eta_interpolation: 'Estimated from schedule',
  } as Record<PositionSource, string>,
  mapLabel: 'Shipment map',
  search: 'Search shipments',
  allCustomers: 'All customers',
  allStatuses: 'All statuses',
  statuses: (n: number) => `${n} statuses`,
  attention: 'Needs attention',
  /** total = distinct shipments (what the attention filter shows); exc and hold are each counted on their own. */
  attentionCount: (total: number, exc: number, hold: number, both: number) =>
    total === 0
      ? 'Nothing needs attention'
      : `${total} need${total === 1 ? 's' : ''} attention · ${exc} exception${exc === 1 ? '' : 's'} · ${hold} customs hold${hold === 1 ? '' : 's'}${both ? ` (${both} both)` : ''}`,
  attentionShort: (total: number) => (total === 0 ? 'Nothing needs attention' : `${total} need${total === 1 ? 's' : ''} attention`),
  shown: (n: number, total: number) => (n === total ? `${fmtInt(n, 'en')} shipments` : `${fmtInt(n, 'en')} of ${fmtInt(total, 'en')}`),
  clearFilters: 'Clear filters',
  filters: 'Filters',
  list: 'List',
  hideList: 'Hide list',
  world: 'World',
  europe: 'Europe',
  finland: 'Finland',
  zoomIn: 'Zoom in',
  zoomOut: 'Zoom out',
  live: 'Live',
  polling: (s: number) => `Updating every ${s} s`,
  connecting: 'Connecting',
  offline: 'Offline',
  updated: (rel: string) => `Updated ${rel}`,
  retry: 'Retry',
  loadFailed: 'Could not load shipments',
  loadFailedShort: 'Load failed',
  retryingIn: (sec: number) => (sec > 0 ? `retrying in ${sec} s` : 'retrying'),
  liveRefreshFailed: 'Live · last refresh failed',
  refreshFailed: (time: string) => `Refresh failed. Showing data from ${time}.`,
  loading: 'Loading shipments',
  emptyNone: 'No shipments yet',
  emptyNoneHint: 'Booked shipments appear here as soon as they have a route.',
  emptyFiltered: 'No shipments match these filters',
  close: 'Close',
  openShipment: 'Open shipment',
  route: 'Route',
  eta: 'ETA',
  etd: 'ETD',
  ata: 'Arrived',
  atd: 'Departed',
  position: 'Position',
  progress: 'Progress',
  cargo: 'Cargo',
  legs: 'Legs',
  customer: 'Customer',
  carrier: 'Carrier',
  current: 'Current',
  exceptionNote: 'Exception reported on this shipment',
  holdNote: 'Held by customs',
  estimated: 'Estimated',
  stale: 'Stale',
  unknown: 'Unavailable',
  teu: 'TEU',
  legend: 'Legend',
  moving: 'Live position',
  ago: (v: string) => `${v} ago`,
  inTime: (v: string) => `in ${v}`,
  now: 'just now',
  fps: 'FPS',
};

type Strings = typeof en;

const fi: Strings = {
  ...en,
  mode: { sea: 'Meri', road: 'Maantie', air: 'Lento', rail: 'Rautatie', multimodal: 'Yhdistetty' },
  status: {
    booked: 'Varattu',
    picked_up: 'Noudettu',
    in_transit: 'Kuljetuksessa',
    at_hub: 'Terminaalissa',
    customs_hold: 'Tullin pysäyttämä',
    out_for_delivery: 'Jakelussa',
    delivered: 'Toimitettu',
    exception: 'Poikkeama',
  },
  source: { adsb: 'ADS-B-lento', ais: 'AIS', gps: 'GPS', carrier_api: 'Kuljettajan syöte', eta_interpolation: 'Arvio aikataulusta' },
  mapLabel: 'Lähetyskartta',
  search: 'Hae lähetyksiä',
  allCustomers: 'Kaikki asiakkaat',
  allStatuses: 'Kaikki tilat',
  statuses: (n) => `${n} tilaa`,
  attention: 'Vaatii huomiota',
  attentionCount: (total, exc, hold, both) =>
    total === 0
      ? 'Ei huomioitavaa'
      : `${total} vaatii huomiota · ${exc} poikkeama${exc === 1 ? '' : 'a'} · ${hold} tullipysäytys${hold === 1 ? '' : 'tä'}${both ? ` (${both} molempia)` : ''}`,
  attentionShort: (total) => (total === 0 ? 'Ei huomioitavaa' : `${total} vaatii huomiota`),
  shown: (n, total) => (n === total ? `${fmtInt(n, 'fi')} lähetystä` : `${fmtInt(n, 'fi')} / ${fmtInt(total, 'fi')}`),
  clearFilters: 'Tyhjennä suodattimet',
  filters: 'Suodattimet',
  list: 'Lista',
  hideList: 'Piilota lista',
  world: 'Maailma',
  europe: 'Eurooppa',
  finland: 'Suomi',
  zoomIn: 'Lähennä',
  zoomOut: 'Loitonna',
  live: 'Reaaliaikainen',
  polling: (s) => `Päivittyy ${s} s välein`,
  connecting: 'Yhdistetään',
  offline: 'Ei yhteyttä',
  updated: (rel) => `Päivitetty ${rel}`,
  retry: 'Yritä uudelleen',
  loadFailed: 'Lähetysten lataus epäonnistui',
  loadFailedShort: 'Lataus epäonnistui',
  retryingIn: (sec) => (sec > 0 ? `uusi yritys ${sec} s kuluttua` : 'yritetään uudelleen'),
  liveRefreshFailed: 'Reaaliaikainen · päivitys epäonnistui',
  refreshFailed: (time) => `Päivitys epäonnistui. Tiedot klo ${time}.`,
  loading: 'Ladataan lähetyksiä',
  emptyNone: 'Ei vielä lähetyksiä',
  emptyNoneHint: 'Varatut lähetykset näkyvät tässä, kun niillä on reitti.',
  emptyFiltered: 'Ei suodattimia vastaavia lähetyksiä',
  close: 'Sulje',
  openShipment: 'Avaa lähetys',
  route: 'Reitti',
  eta: 'ETA',
  etd: 'ETD',
  ata: 'Saapui',
  atd: 'Lähti',
  position: 'Sijainti',
  progress: 'Edistyminen',
  cargo: 'Rahti',
  legs: 'Osuudet',
  customer: 'Asiakas',
  carrier: 'Kuljettaja',
  current: 'Nyt',
  exceptionNote: 'Lähetyksellä on poikkeama',
  holdNote: 'Tulli on pysäyttänyt lähetyksen',
  estimated: 'Arvio',
  stale: 'Vanhentunut',
  unknown: 'Ei saatavilla',
  teu: 'TEU',
  legend: 'Selite',
  moving: 'Sijainti nyt',
  ago: (v) => `${v} sitten`,
  inTime: (v) => `${v} päästä`,
  now: 'juuri nyt',
  fps: 'FPS',
};

export const strings: Record<Locale, Strings> = { en, fi };
export type { Strings };

export const LEG_MODES: LegMode[] = ['sea', 'road', 'air', 'rail'];
export const MODES: Mode[] = ['sea', 'road', 'air', 'rail', 'multimodal'];
export const STATUSES: Status[] = [
  'booked',
  'picked_up',
  'in_transit',
  'at_hub',
  'customs_hold',
  'out_for_delivery',
  'delivered',
  'exception',
];

export function fmtInt(n: number, locale: Locale): string {
  return new Intl.NumberFormat(locale === 'fi' ? 'fi-FI' : 'en-GB').format(n);
}

export function fmtDate(iso: string | null | undefined, locale: Locale, withTime = true): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return new Intl.DateTimeFormat(locale === 'fi' ? 'fi-FI' : 'en-GB', {
    day: 'numeric',
    month: 'short',
    ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}),
    timeZone: 'Europe/Helsinki',
  }).format(d);
}

export function fmtTime(ms: number, locale: Locale): string {
  return new Intl.DateTimeFormat(locale === 'fi' ? 'fi-FI' : 'en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(ms));
}

/** Compact duration: 45 s, 12 min, 5 h, 3 d. */
export function fmtSpan(ms: number, locale: Locale): string {
  const s = Math.abs(ms) / 1000;
  const fi = locale === 'fi';
  if (s < 60) return `${Math.max(1, Math.round(s))} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} ${fi ? 't' : 'h'}`;
  return `${Math.round(s / 86400)} ${fi ? 'pv' : 'd'}`;
}

export function fmtRelative(iso: string | number | null | undefined, now: number, locale: Locale): string {
  if (iso == null) return '';
  const t = typeof iso === 'number' ? iso : new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const diff = t - now;
  const S = strings[locale];
  if (Math.abs(diff) < 15000) return S.now;
  return diff < 0 ? S.ago(fmtSpan(diff, locale)) : S.inTime(fmtSpan(diff, locale));
}
