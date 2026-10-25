// Geometry helpers. Everything works on [lon, lat] degrees; distances in km.

const R = 6371.0088;
const RAD = Math.PI / 180;

export type LonLat = [number, number];
export type LonLatAlt = [number, number, number];

export function haversineKm(a: LonLat | number[], b: LonLat | number[]): number {
  const dLat = (b[1] - a[1]) * RAD;
  const dLon = (b[0] - a[0]) * RAD;
  const s =
    Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * RAD) * Math.cos(b[1] * RAD) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
}

/** Great-circle interpolation between two points, f in 0..1. */
export function interpolateGC(a: LonLat, b: LonLat, f: number): LonLat {
  const φ1 = a[1] * RAD, λ1 = a[0] * RAD, φ2 = b[1] * RAD, λ2 = b[0] * RAD;
  const d = haversineKm(a, b) / R;
  if (d < 1e-9) return [a[0], a[1]];
  const A = Math.sin((1 - f) * d) / Math.sin(d);
  const B = Math.sin(f * d) / Math.sin(d);
  const x = A * Math.cos(φ1) * Math.cos(λ1) + B * Math.cos(φ2) * Math.cos(λ2);
  const y = A * Math.cos(φ1) * Math.sin(λ1) + B * Math.cos(φ2) * Math.sin(λ2);
  const z = A * Math.sin(φ1) + B * Math.sin(φ2);
  return [Math.atan2(y, x) / RAD, Math.atan2(z, Math.sqrt(x * x + y * y)) / RAD];
}

/** Insert great-circle points so that no segment is longer than maxKm. */
export function densify(path: LonLat[], maxKm: number): LonLat[] {
  if (path.length < 2) return path.slice();
  const out: LonLat[] = [path[0]];
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1], b = path[i];
    const n = Math.ceil(haversineKm(a, b) / maxKm);
    for (let k = 1; k < n; k++) out.push(interpolateGC(a, b, k / n));
    out.push(b);
  }
  return out;
}

/** Cumulative distance (km) at each vertex. */
export function cumulative(path: LonLat[]): Float64Array {
  const cum = new Float64Array(path.length);
  for (let i = 1; i < path.length; i++) cum[i] = cum[i - 1] + haversineKm(path[i - 1], path[i]);
  return cum;
}

/**
 * Project a point onto a polyline (equirectangular local approximation per segment,
 * accurate enough at lane scale). Returns the distance along the path in km and the
 * segment index, plus the distance off the path.
 */
export function projectOnPath(
  path: LonLat[],
  cum: Float64Array,
  p: LonLat,
): { along: number; seg: number; t: number; offKm: number } {
  let best = { along: 0, seg: 0, t: 0, offKm: Infinity };
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1], b = path[i];
    const k = Math.cos(((a[1] + b[1]) / 2) * RAD);
    let dx = b[0] - a[0];
    if (dx > 180) dx -= 360;
    if (dx < -180) dx += 360;
    let px = p[0] - a[0];
    if (px > 180) px -= 360;
    if (px < -180) px += 360;
    const ax = dx * k, ay = b[1] - a[1];
    const qx = px * k, qy = p[1] - a[1];
    const len2 = ax * ax + ay * ay;
    const t = len2 > 0 ? Math.max(0, Math.min(1, (qx * ax + qy * ay) / len2)) : 0;
    const ex = qx - ax * t, ey = qy - ay * t;
    const off = Math.sqrt(ex * ex + ey * ey) * RAD * R;
    if (off < best.offKm) {
      best = { along: cum[i - 1] + (cum[i] - cum[i - 1]) * t, seg: i - 1, t, offKm: off };
    }
  }
  return best;
}

/** Point at a distance (km) along a path. */
export function pointAlong(path: LonLat[], cum: Float64Array, km: number): LonLat {
  if (path.length === 0) return [0, 0];
  if (km <= 0) return path[0];
  const total = cum[cum.length - 1];
  if (km >= total) return path[path.length - 1];
  let lo = 0, hi = cum.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (cum[mid] <= km) lo = mid; else hi = mid;
  }
  const seg = cum[hi] - cum[lo];
  const f = seg > 0 ? (km - cum[lo]) / seg : 0;
  return [path[lo][0] + (path[hi][0] - path[lo][0]) * f, path[lo][1] + (path[hi][1] - path[lo][1]) * f];
}

/** Split a path at a distance along it: [travelled, remaining]. */
export function splitPath(path: LonLat[], cum: Float64Array, km: number): [LonLat[], LonLat[]] {
  const total = cum[cum.length - 1];
  if (km <= 0) return [[], path];
  if (km >= total) return [path, []];
  const cut = pointAlong(path, cum, km);
  let i = 0;
  while (i < cum.length && cum[i] <= km) i++;
  return [[...path.slice(0, i), cut], [cut, ...path.slice(i)]];
}

/** Peak altitude (m) of the drawn air arc for a leg of the given length. */
export function airPeakMeters(legKm: number): number {
  return Math.max(50, Math.min(600, legKm * 0.05)) * 1000;
}

/** Altitude (m) at fraction f of an air leg: a smooth arc, zero at both airports. */
export function airAltitude(f: number, peakM: number): number {
  return Math.sin(Math.PI * Math.max(0, Math.min(1, f))) * peakM;
}

export function toArc(path: LonLat[], cum: Float64Array): LonLatAlt[] {
  const total = cum[cum.length - 1] || 1;
  const peak = airPeakMeters(total);
  return path.map((p, i) => [p[0], p[1], airAltitude(cum[i] / total, peak)]);
}

export function bounds(points: LonLat[]): [[number, number], [number, number]] {
  let w = 180, s = 90, e = -180, n = -90;
  for (const p of points) {
    if (p[0] < w) w = p[0];
    if (p[0] > e) e = p[0];
    if (p[1] < s) s = p[1];
    if (p[1] > n) n = p[1];
  }
  return [[w, s], [e, n]];
}

/** Stable identity for a lane geometry so identical legs are drawn once. */
export function laneKey(mode: string, path: LonLat[]): string {
  const f = path[0], l = path[path.length - 1], m = path[path.length >> 1];
  const r = (v: number) => v.toFixed(3);
  return `${mode}|${path.length}|${r(f[0])},${r(f[1])}|${r(m[0])},${r(m[1])}|${r(l[0])},${r(l[1])}`;
}
