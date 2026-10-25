import type { PreparedShipment } from './prepare';
import type { Mode, Status } from './types';
import { MODES, STATUSES } from './i18n';

export interface Filters {
  modes: Mode[];
  statuses: Status[];
  customer: string;
  q: string;
  attention: boolean;
}

export const EMPTY_FILTERS: Filters = { modes: [], statuses: [], customer: '', q: '', attention: false };

export function isFiltered(f: Filters): boolean {
  return f.modes.length > 0 || f.statuses.length > 0 || !!f.customer || !!f.q.trim() || f.attention;
}

/**
 * A shipment matches a mode chip when its own mode or any of its legs uses that mode,
 * so "Sea" also shows sea + road door-to-door moves. "Multimodal" matches the
 * contract's multimodal shipments only.
 */
export function matchesMode(p: PreparedShipment, modes: Mode[]): boolean {
  if (!modes.length) return true;
  if (modes.includes(p.s.mode)) return true;
  return p.s.legs.some((l) => modes.includes(l.mode));
}

export function applyFilters(all: Iterable<PreparedShipment>, f: Filters, skip?: keyof Filters): PreparedShipment[] {
  const q = f.q.trim().toLowerCase();
  const out: PreparedShipment[] = [];
  for (const p of all) {
    if (skip !== 'modes' && !matchesMode(p, f.modes)) continue;
    if (skip !== 'statuses' && f.statuses.length && !f.statuses.includes(p.s.status)) continue;
    if (skip !== 'customer' && f.customer && p.s.customer_name !== f.customer) continue;
    if (skip !== 'attention' && f.attention && !p.attention) continue;
    if (q && !p.search.includes(q)) continue;
    out.push(p);
  }
  return out;
}

export interface FacetCounts {
  mode: Record<Mode, number>;
  status: Record<Status, number>;
  /** Shipments with status "exception" or has_exception, counted on their own. */
  exceptions: number;
  /** Shipments with status "customs_hold", counted on their own (same as the status facet). */
  holds: number;
  /** Customs holds that also carry an exception: counted in both numbers above. */
  both: number;
  /** Distinct shipments needing attention: exactly what the attention filter shows. */
  attention: number;
}

export function isException(p: PreparedShipment): boolean {
  return p.s.status === 'exception' || p.s.has_exception;
}

/** Counts for each facet, computed with every other filter applied (faceted search). */
export function facetCounts(all: PreparedShipment[], f: Filters): FacetCounts {
  const mode = Object.fromEntries(MODES.map((m) => [m, 0])) as Record<Mode, number>;
  const status = Object.fromEntries(STATUSES.map((s) => [s, 0])) as Record<Status, number>;
  for (const p of applyFilters(all, f, 'modes')) {
    for (const m of MODES) if (matchesMode(p, [m])) mode[m]++;
  }
  for (const p of applyFilters(all, f, 'statuses')) status[p.s.status] = (status[p.s.status] ?? 0) + 1;
  let exceptions = 0, holds = 0, both = 0, attention = 0;
  // Same base set the attention filter uses, so the chip and the filtered list agree.
  for (const p of applyFilters(all, f, 'attention')) {
    const exc = isException(p);
    const hold = p.s.status === 'customs_hold';
    if (exc) exceptions++;
    if (hold) holds++;
    if (exc && hold) both++;
    if (p.attention) attention++;
  }
  return { mode, status, exceptions, holds, both, attention };
}

/** Attention first, then soonest ETA; delivered last. */
export function sortForList(list: PreparedShipment[]): PreparedShipment[] {
  const rank = (p: PreparedShipment) => (p.attention ? 0 : p.s.status === 'delivered' ? 2 : 1);
  return [...list].sort((a, b) => {
    const r = rank(a) - rank(b);
    if (r) return r;
    return rank(a) === 2 ? b.etaMs - a.etaMs : a.etaMs - b.etaMs;
  });
}

export function loadFilters(key: string | null): Filters {
  if (!key || typeof localStorage === 'undefined') return EMPTY_FILTERS;
  try {
    const raw = JSON.parse(localStorage.getItem(key) ?? 'null');
    if (!raw || typeof raw !== 'object') return EMPTY_FILTERS;
    return {
      modes: Array.isArray(raw.modes) ? raw.modes.filter((m: Mode) => MODES.includes(m)) : [],
      statuses: Array.isArray(raw.statuses) ? raw.statuses.filter((s: Status) => STATUSES.includes(s)) : [],
      customer: typeof raw.customer === 'string' ? raw.customer : '',
      q: typeof raw.q === 'string' ? raw.q : '',
      attention: raw.attention === true,
    };
  } catch {
    return EMPTY_FILTERS;
  }
}

export function saveFilters(key: string | null, f: Filters): void {
  if (!key || typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(key, JSON.stringify(f));
  } catch {
    /* storage full or blocked: filters stay session-only */
  }
}
