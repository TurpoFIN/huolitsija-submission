// Turns contract shipments into render-ready records: densified air arcs, cumulative
// distances, lane identities, the current leg and an attention flag.

import type { LegMode, Shipment, Status } from './types';
import {
  airAltitude,
  airPeakMeters,
  cumulative,
  densify,
  laneKey,
  projectOnPath,
  toArc,
  type LonLat,
  type LonLatAlt,
} from './geo';

export interface PreparedLeg {
  mode: LegMode;
  path: LonLat[];
  cum: Float64Array;
  km: number;
  key: string;
  /** Air legs only: the path lifted into an arc. */
  arc: LonLatAlt[] | null;
  peak: number;
}

export interface PreparedShipment {
  s: Shipment;
  legs: PreparedLeg[];
  totalKm: number;
  currentLeg: number;
  attention: boolean;
  etaMs: number;
  search: string;
}

const legCache = new Map<string, PreparedLeg>();

function prepareLeg(mode: LegMode, raw: [number, number][]): PreparedLeg {
  const base: LonLat[] = raw.length >= 2 ? raw.map((p) => [p[0], p[1]] as LonLat) : [];
  const key = base.length >= 2 ? laneKey(mode, base) : `${mode}|empty`;
  const cached = legCache.get(key);
  if (cached) return cached;
  // Air legs may arrive with only a handful of great-circle points; make the arc smooth.
  const path = mode === 'air' && base.length >= 2 ? densify(base, 80) : base;
  const cum = path.length ? cumulative(path) : new Float64Array(0);
  const km = cum.length ? cum[cum.length - 1] : 0;
  const leg: PreparedLeg = {
    mode,
    path,
    cum,
    km,
    key,
    arc: mode === 'air' && path.length >= 2 ? toArc(path, cum) : null,
    peak: mode === 'air' ? airPeakMeters(km) : 0,
  };
  if (legCache.size > 20000) legCache.clear();
  legCache.set(key, leg);
  return leg;
}

export function isAttention(status: Status, hasException: boolean): boolean {
  return status === 'exception' || status === 'customs_hold' || hasException;
}

export function currentLegIndex(s: Shipment): number {
  const legs = s.legs;
  if (!legs.length) return -1;
  for (let i = 0; i < legs.length; i++) if (legs[i].atd && !legs[i].ata) return i;
  let lastArrived = -1;
  for (let i = 0; i < legs.length; i++) if (legs[i].ata) lastArrived = i;
  if (lastArrived === -1) return 0;
  return lastArrived;
}

/** Leg index covering `progress` (0..1 of total route distance). */
export function legFromProgress(legs: PreparedLeg[], progress: number, fallback: number): number {
  const total = legs.reduce((sum, l) => sum + l.km, 0);
  if (!(total > 0) || !Number.isFinite(progress)) return fallback;
  const km = Math.max(0, Math.min(1, progress)) * total;
  let acc = 0;
  for (let i = 0; i < legs.length; i++) {
    acc += legs[i].km;
    if (km <= acc + 1e-6) return i;
  }
  return legs.length - 1;
}

export function prepare(s: Shipment): PreparedShipment {
  const legs = [...s.legs].sort((a, b) => a.seq - b.seq).map((l) => prepareLeg(l.mode, l.path ?? []));
  const sortedRaw = [...s.legs].sort((a, b) => a.seq - b.seq);
  const sorted: Shipment = { ...s, legs: sortedRaw };
  return {
    s: sorted,
    legs,
    totalKm: legs.reduce((sum, l) => sum + l.km, 0),
    currentLeg: legFromProgress(legs, s.progress, currentLegIndex(sorted)),
    attention: isAttention(s.status, s.has_exception),
    etaMs: Date.parse(s.eta) || 0,
    search: [
      s.reference,
      s.customer_name,
      s.origin?.name,
      s.origin?.unlocode,
      s.destination?.name,
      s.destination?.unlocode,
      ...s.legs.map((l) => l.carrier),
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase(),
  };
}

/** Mode used to colour the marker: the mode of the leg the shipment is on now. */
export function activeMode(p: PreparedShipment): LegMode {
  if (p.s.mode !== 'multimodal') return p.s.mode as LegMode;
  const leg = p.s.legs[p.currentLeg];
  return leg ? leg.mode : 'road';
}

/** Altitude (m) of the marker: only air legs are lifted, onto the drawn arc. */
export function markerAltitude(p: PreparedShipment, lon: number, lat: number): number {
  const leg = p.legs[p.currentLeg];
  if (!leg || leg.mode !== 'air' || leg.path.length < 2) return 0;
  const status = p.s.status;
  if (status === 'delivered' || status === 'customs_hold' || status === 'at_hub' || status === 'booked') return 0;
  const proj = projectOnPath(leg.path, leg.cum, [lon, lat]);
  if (proj.offKm > 150) return 0;
  return airAltitude(proj.along / (leg.km || 1), leg.peak);
}

/** Distance travelled along the current leg, for splitting the selected route. */
export function travelledOnCurrentLeg(p: PreparedShipment): number {
  const leg = p.legs[p.currentLeg];
  if (!leg || leg.path.length < 2) return 0;
  return projectOnPath(leg.path, leg.cum, [p.s.position.lon, p.s.position.lat]).along;
}
