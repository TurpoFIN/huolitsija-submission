// Owns the deck.gl layers drawn into the MapLibre globe and the per-frame animation
// of marker positions. Lives outside React: React tells it what is visible and
// selected; it rebuilds typed arrays only when those change and only rewrites
// positions per frame.

import type { Map as MLMap } from 'maplibre-gl';
import type { MapboxOverlay } from '@deck.gl/mapbox';
import type { Layer, PickingInfo } from '@deck.gl/core';
import { PathLayer, ScatterplotLayer } from '@deck.gl/layers';
import maplibregl from 'maplibre-gl';
import { PathStyleExtension } from '@deck.gl/extensions';
import type { Palette, RGBA } from './colors';
import { activeMode, markerAltitude, travelledOnCurrentLeg, type PreparedShipment } from './prepare';
import { splitPath, type LonLat } from './geo';
import type { PositionChange, ShipmentFeed } from './feed';

interface Anim {
  fromLon: number;
  fromLat: number;
  fromAlt: number;
  toLon: number;
  toLat: number;
  toAlt: number;
  t0: number;
  dur: number;
  lon: number;
  lat: number;
  alt: number;
  lastUpdate: number;
}

interface LaneDatum {
  path: number[][];
  color: RGBA;
  width: number;
}

interface RouteDatum {
  path: number[][];
  color: RGBA;
}

export interface RendererCallbacks {
  onHover: (id: string | null, x: number, y: number) => void;
  onClick: (id: string | null) => void;
}

export interface FrameStats {
  fps: number;
  frameMsP95: number;
  frames: number;
  markers: number;
  lanes: number;
}

// Depth bias keeps flat geometry above MapLibre's tessellated globe surface (no
// z-fighting stripes) while the far side of the globe still occludes it.
const SURFACE = { depthBias: -200, depthBiasSlopeScale: -4 } as const;
const dashExt = new PathStyleExtension({ dash: true });

export class MapRenderer {
  private anims = new Map<string, Anim>();
  private visible: PreparedShipment[] = [];
  private positions = new Float32Array(0);
  private fill = new Uint8Array(0);
  private line = new Uint8Array(0);
  private radius = new Float32Array(0);
  private haloIdx: number[] = [];
  private haloPos = new Float32Array(0);
  private haloColor = new Uint8Array(0);
  private lanes: LaneDatum[] = [];
  private route: { done: RouteDatum[]; ahead: RouteDatum[]; ends: { pos: number[]; name: string }[] } | null = null;
  private selectedId: string | null = null;
  private hoveredId: string | null = null;
  private palette: Palette;
  private raf = 0;
  private dirty = true;
  private destroyed = false;
  private labelBeforeId: string | undefined;
  private frameTimes: number[] = [];
  private lastFrame = 0;
  private frameCount = 0;
  private reducedMotion: boolean;
  private unsub: () => void;
  private zoomScale = 1;
  private endMarkers: maplibregl.Marker[] = [];
  private endKey = '';

  constructor(
    private map: MLMap,
    private overlay: MapboxOverlay,
    private feed: ShipmentFeed,
    palette: Palette,
    private cb: RendererCallbacks,
  ) {
    this.palette = palette;
    this.reducedMotion =
      typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    this.unsub = feed.onPositions((c) => this.onPositions(c));
    this.overlay.setProps({
      pickingRadius: 8,
      onHover: (info: PickingInfo) => this.handleHover(info),
      onClick: (info: PickingInfo) => this.handleClick(info),
    });
    const onZoom = () => {
      const z = this.map.getZoom();
      const s = z < 3 ? 1 : z > 7 ? 1.45 : 1 + ((z - 3) / 4) * 0.45;
      if (Math.abs(s - this.zoomScale) > 0.01) {
        this.zoomScale = s;
        this.dirty = true;
      }
    };
    this.map.on('zoom', onZoom);
    onZoom();
    this.findLabelLayer();
    this.loop = this.loop.bind(this);
    this.raf = requestAnimationFrame(this.loop);
  }

  destroy(): void {
    this.endMarkers.forEach((m) => m.remove());
    this.destroyed = true;
    cancelAnimationFrame(this.raf);
    this.unsub();
  }

  /** Call after a style (theme) change so lanes stay under the place labels. */
  findLabelLayer(): void {
    const layers = this.map.getStyle()?.layers ?? [];
    this.labelBeforeId = layers.find((l) => l.type === 'symbol')?.id;
    this.dirty = true;
  }

  setPalette(p: Palette): void {
    this.palette = p;
    this.rebuild();
  }

  setVisible(list: PreparedShipment[]): void {
    this.visible = list;
    this.rebuild();
  }

  setSelected(id: string | null): void {
    this.selectedId = id;
    this.buildRoute();
    this.rebuildStyle();
  }

  setHovered(id: string | null): void {
    if (id === this.hoveredId) return;
    this.hoveredId = id;
    this.rebuildStyle();
  }

  /** Current animated position of a shipment (for camera moves). */
  positionOf(id: string): [number, number] | null {
    const a = this.anims.get(id);
    if (a) return [a.lon, a.lat];
    const p = this.feed.shipments.get(id);
    return p ? [p.s.position.lon, p.s.position.lat] : null;
  }

  stats(): FrameStats {
    const t = [...this.frameTimes].sort((a, b) => a - b);
    const mean = t.length ? t.reduce((s, v) => s + v, 0) / t.length : 0;
    return {
      fps: mean ? Math.round((1000 / mean) * 10) / 10 : 0,
      frameMsP95: t.length ? Math.round(t[Math.floor(t.length * 0.95)] * 10) / 10 : 0,
      frames: this.frameCount,
      markers: this.visible.length,
      lanes: this.lanes.length,
    };
  }

  // ---- data ----------------------------------------------------------------

  private ensureAnim(p: PreparedShipment): Anim {
    let a = this.anims.get(p.s.id);
    if (!a) {
      const { lon, lat } = p.s.position;
      const alt = markerAltitude(p, lon, lat);
      a = { fromLon: lon, fromLat: lat, fromAlt: alt, toLon: lon, toLat: lat, toAlt: alt, t0: 0, dur: 0, lon, lat, alt, lastUpdate: performance.now() };
      this.anims.set(p.s.id, a);
    }
    return a;
  }

  private onPositions(changes: PositionChange[]): void {
    const now = performance.now();
    for (const { p } of changes) {
      const a = this.ensureAnim(p);
      const { lon, lat } = p.s.position;
      const alt = markerAltitude(p, lon, lat);
      const gap = now - a.lastUpdate;
      a.fromLon = a.lon;
      a.fromLat = a.lat;
      a.fromAlt = a.alt;
      a.toLon = lon;
      a.toLat = lat;
      a.toAlt = alt;
      a.t0 = now;
      // Glide over the interval between reports (bounded), so movement is continuous
      // but the marker is never more than one report behind the real position.
      a.dur = this.reducedMotion ? 0 : Math.max(400, Math.min(gap, 5000));
      a.lastUpdate = now;
      if (a.dur === 0) {
        a.lon = lon;
        a.lat = lat;
        a.alt = alt;
      }
    }
    if (this.selectedId && changes.some((c) => c.p.s.id === this.selectedId)) this.buildRoute();
    this.dirty = true;
  }

  private rebuild(): void {
    const n = this.visible.length;
    if (this.positions.length !== n * 3) {
      this.positions = new Float32Array(n * 3);
      this.fill = new Uint8Array(n * 4);
      this.line = new Uint8Array(n * 4);
      this.radius = new Float32Array(n);
    }
    const live = new Set<string>();
    for (const p of this.visible) {
      live.add(p.s.id);
      this.ensureAnim(p);
    }
    for (const id of [...this.anims.keys()]) if (!live.has(id) && !this.feed.shipments.has(id)) this.anims.delete(id);
    this.buildLanes();
    this.buildRoute();
    this.rebuildStyle();
  }

  private rebuildStyle(): void {
    const P = this.palette;
    const halo: number[] = [];
    // Draw order: delivered first, attention last, so problems are never hidden.
    this.visible.sort((a, b) => rank(a) - rank(b));
    for (let i = 0; i < this.visible.length; i++) {
      const p = this.visible[i];
      const s = p.s;
      const mode = activeMode(p);
      const c = P.mode[mode];
      let fill: RGBA = c;
      let line: RGBA = P.surface;
      let r = 3.5;
      if (s.status === 'delivered') {
        fill = P.surface;
        line = P.neutral;
        r = 2.5;
      } else if (s.status === 'booked') {
        fill = P.surface;
        line = c;
        r = 3;
      }
      if (s.status === 'exception' || s.has_exception) {
        line = P.danger;
        r = 5;
        halo.push(i);
      } else if (s.status === 'customs_hold') {
        line = P.warning;
        r = 5;
        halo.push(i);
      }
      if (s.id === this.selectedId || s.id === this.hoveredId) r += 2;
      this.fill.set(fill, i * 4);
      this.line.set(line, i * 4);
      this.radius[i] = r;
    }
    this.haloIdx = halo;
    if (this.haloPos.length !== halo.length * 3) {
      this.haloPos = new Float32Array(halo.length * 3);
      this.haloColor = new Uint8Array(halo.length * 4);
    }
    halo.forEach((vi, k) => {
      const s = this.visible[vi].s;
      this.haloColor.set(s.status === 'exception' || s.has_exception ? P.danger : P.warning, k * 4);
    });
    this.dirty = true;
  }

  private buildLanes(): void {
    const P = this.palette;
    const acc = new Map<string, { path: number[][]; mode: keyof Palette['mode']; count: number }>();
    for (const p of this.visible) {
      if (p.s.status === 'delivered') continue;
      for (const leg of p.legs) {
        if (leg.path.length < 2) continue;
        const e = acc.get(leg.key);
        if (e) e.count++;
        else acc.set(leg.key, { path: leg.arc ?? leg.path, mode: leg.mode, count: 1 });
      }
    }
    const lanes: LaneDatum[] = [];
    for (const e of acc.values()) {
      const k = Math.log2(e.count + 1);
      const base = P.mode[e.mode];
      const alpha = Math.round(Math.min(P.dark ? 190 : 170, (P.dark ? 70 : 60) + 28 * k));
      lanes.push({ path: e.path, color: [base[0], base[1], base[2], alpha], width: Math.min(3, 1 + 0.45 * k) });
    }
    // Busy lanes on top.
    lanes.sort((a, b) => a.width - b.width);
    this.lanes = lanes;
  }

  private buildRoute(): void {
    const p = this.selectedId ? this.feed.shipments.get(this.selectedId) : null;
    if (!p) {
      this.route = null;
      this.setEndLabels([]);
      this.dirty = true;
      return;
    }
    const done: RouteDatum[] = [];
    const ahead: RouteDatum[] = [];
    const delivered = p.s.status === 'delivered';
    p.legs.forEach((leg, i) => {
      if (leg.path.length < 2) return;
      const color = this.palette.mode[leg.mode];
      if (delivered || i < p.currentLeg) done.push({ path: liftSplit(leg, leg.path), color });
      else if (i > p.currentLeg) ahead.push({ path: liftSplit(leg, leg.path), color });
      else {
        const km = p.s.status === 'booked' ? 0 : travelledOnCurrentLeg(p);
        const [a, b] = splitPath(leg.path, leg.cum, km);
        if (a.length >= 2) done.push({ path: liftSplit(leg, a), color });
        if (b.length >= 2) ahead.push({ path: liftSplit(leg, b), color });
      }
    });
    const o = p.s.origin, d = p.s.destination;
    this.route = {
      done,
      ahead,
      ends: [
        { pos: [o.lon, o.lat, 0], name: o.name },
        { pos: [d.lon, d.lat, 0], name: d.name },
      ],
    };
    this.setEndLabels(this.route.ends);
    this.dirty = true;
  }

  /** Origin / destination names as HTML markers: crisp text, hidden behind the globe. */
  private setEndLabels(ends: { pos: number[]; name: string }[]): void {
    const key = ends.map((e) => `${e.name}@${e.pos[0]},${e.pos[1]}`).join('|');
    if (this.endKey === key) return;
    this.endKey = key;
    this.endMarkers.forEach((m) => m.remove());
    this.endMarkers = ends.map((e) => {
      const el = document.createElement('div');
      el.className = 'huo-endlabel';
      el.textContent = e.name;
      const m = new maplibregl.Marker({ element: el, anchor: 'bottom', offset: [0, -9], opacityWhenCovered: '0' })
        .setLngLat([e.pos[0], e.pos[1]])
        .addTo(this.map);
      // Visual only: the panel carries origin and destination for assistive tech.
      el.removeAttribute('aria-label');
      el.removeAttribute('role');
      el.setAttribute('aria-hidden', 'true');
      return m;
    });
  }

  // ---- interaction ---------------------------------------------------------

  private idAt(info: PickingInfo): string | null {
    if (!info.layer || info.index < 0) return null;
    if (info.layer.id === 'huo-markers') return this.visible[info.index]?.s.id ?? null;
    if (info.layer.id === 'huo-halo') return this.visible[this.haloIdx[info.index]]?.s.id ?? null;
    return null;
  }

  private handleHover(info: PickingInfo): void {
    const id = this.idAt(info);
    this.cb.onHover(id, info.x, info.y);
    this.map.getCanvas().style.cursor = id ? 'pointer' : '';
  }

  private handleClick(info: PickingInfo): void {
    this.cb.onClick(this.idAt(info));
  }

  // ---- frame loop ----------------------------------------------------------

  private loop(): void {
    if (this.destroyed) return;
    // performance.now(), not the rAF timestamp: headless/unsynced frames can share a timestamp.
    const t = performance.now();
    this.raf = requestAnimationFrame(this.loop);
    if (this.lastFrame) {
      this.frameTimes.push(t - this.lastFrame);
      if (this.frameTimes.length > 240) this.frameTimes.shift();
    }
    this.lastFrame = t;
    this.frameCount++;

    let moving = false;
    for (let i = 0; i < this.visible.length; i++) {
      const a = this.anims.get(this.visible[i].s.id);
      if (!a) continue;
      if (a.dur > 0 && t < a.t0 + a.dur) {
        const f = Math.max(0, (t - a.t0) / a.dur);
        let dLon = a.toLon - a.fromLon;
        if (dLon > 180) dLon -= 360;
        if (dLon < -180) dLon += 360;
        a.lon = a.fromLon + dLon * f;
        a.lat = a.fromLat + (a.toLat - a.fromLat) * f;
        a.alt = a.fromAlt + (a.toAlt - a.fromAlt) * f;
        moving = true;
      } else if (a.lon !== a.toLon || a.lat !== a.toLat) {
        a.lon = a.toLon;
        a.lat = a.toLat;
        a.alt = a.toAlt;
        moving = true;
      }
      this.positions[i * 3] = a.lon;
      this.positions[i * 3 + 1] = a.lat;
      this.positions[i * 3 + 2] = a.alt;
    }
    const pulse = this.haloIdx.length > 0 && !this.reducedMotion;
    if (!moving && !pulse && !this.dirty) return;
    this.haloIdx.forEach((vi, k) => {
      this.haloPos[k * 3] = this.positions[vi * 3];
      this.haloPos[k * 3 + 1] = this.positions[vi * 3 + 1];
      this.haloPos[k * 3 + 2] = this.positions[vi * 3 + 2];
    });
    this.dirty = false;
    this.overlay.setProps({ layers: this.layers(t) });
  }

  private layers(t: number): Layer[] {
    const P = this.palette;
    const n = this.visible.length;
    const phase = this.reducedMotion ? 0.5 : (t % 1800) / 1800;
    const out: Layer[] = [
      new PathLayer<LaneDatum>({
        id: 'huo-lanes',
        data: this.lanes,
        getPath: (d: LaneDatum) => d.path as never,
        getColor: (d: LaneDatum) => d.color,
        getWidth: (d: LaneDatum) => d.width,
        widthUnits: 'pixels',
        capRounded: true,
        jointRounded: true,
        parameters: SURFACE,
        beforeId: this.labelBeforeId,
        updateTriggers: { getColor: [this.lanes], getWidth: [this.lanes] },
      } as never),
    ];
    if (this.route) {
      out.push(
        new PathLayer<RouteDatum>({
          id: 'huo-route-done',
          data: this.route.done,
          getPath: (d: RouteDatum) => d.path as never,
          getColor: (d: RouteDatum) => d.color,
          getWidth: 3,
          widthUnits: 'pixels',
          capRounded: true,
          jointRounded: true,
          parameters: SURFACE,
        } as never),
        new PathLayer<RouteDatum>({
          id: 'huo-route-ahead',
          data: this.route.ahead,
          getPath: (d: RouteDatum) => d.path as never,
          getColor: (d: RouteDatum) => [d.color[0], d.color[1], d.color[2], 230],
          getWidth: 2.5,
          widthUnits: 'pixels',
          getDashArray: [3, 2.5],
          dashJustified: true,
          extensions: [dashExt],
          parameters: SURFACE,
        } as never),
        new ScatterplotLayer<{ pos: number[]; name: string }>({
          id: 'huo-ends',
          data: this.route.ends,
          getPosition: (d: { pos: number[]; name: string }) => d.pos as never,
          getRadius: 5,
          radiusUnits: 'pixels',
          stroked: true,
          filled: true,
          getFillColor: P.surface,
          getLineColor: P.text,
          lineWidthUnits: 'pixels',
          getLineWidth: 2,
          billboard: true,
          parameters: SURFACE,
        } as never),
      );
    }
    if (this.haloIdx.length) {
      const k = this.haloIdx.length;
      out.push(
        new ScatterplotLayer({
          id: 'huo-halo',
          data: {
            length: k,
            attributes: {
              getPosition: { value: this.haloPos, size: 3 },
              getLineColor: { value: this.haloColor, size: 4, normalized: true },
            },
          },
          getRadius: 7,
          radiusUnits: 'pixels',
          radiusScale: this.zoomScale * (1 + phase * 1.1),
          stroked: true,
          filled: false,
          lineWidthUnits: 'pixels',
          getLineWidth: 2,
          opacity: this.reducedMotion ? 0.6 : 0.85 * (1 - phase),
          billboard: true,
          pickable: false,
          parameters: SURFACE,
        } as never),
      );
    }
    out.push(
      new ScatterplotLayer({
        id: 'huo-markers',
        data: {
          length: n,
          attributes: {
            getPosition: { value: this.positions, size: 3 },
            getFillColor: { value: this.fill, size: 4, normalized: true },
            getLineColor: { value: this.line, size: 4, normalized: true },
            getRadius: { value: this.radius, size: 1 },
          },
        },
        radiusUnits: 'pixels',
        radiusScale: this.zoomScale,
        stroked: true,
        filled: true,
        lineWidthUnits: 'pixels',
        getLineWidth: 1.25,
        billboard: true,
        pickable: true,
        autoHighlight: false,
        parameters: SURFACE,
      } as never),
    );
    const sel = this.selectedId ? this.visible.findIndex((p) => p.s.id === this.selectedId) : -1;
    if (sel >= 0) {
      out.push(
        new ScatterplotLayer({
          id: 'huo-selected',
          data: [0],
          getPosition: () => [this.positions[sel * 3], this.positions[sel * 3 + 1], this.positions[sel * 3 + 2]],
          getRadius: this.radius[sel] + 5,
          radiusUnits: 'pixels',
          radiusScale: this.zoomScale,
          stroked: true,
          filled: false,
          getLineColor: P.text,
          lineWidthUnits: 'pixels',
          getLineWidth: 2,
          billboard: true,
          parameters: SURFACE,
          updateTriggers: { getPosition: [t] },
        } as never),
      );
    }
    return out;
  }
}

function rank(p: PreparedShipment): number {
  if (p.s.status === 'delivered') return 0;
  if (p.s.status === 'booked') return 1;
  if (p.attention) return 3;
  return 2;
}

function arcAltAt(arc: number[][], path: LonLat[], q: LonLat): number {
  let best = 0, bd = Infinity;
  for (let i = 0; i < path.length; i++) {
    const d = (path[i][0] - q[0]) ** 2 + (path[i][1] - q[1]) ** 2;
    if (d < bd) {
      bd = d;
      best = i;
    }
  }
  return arc[best][2];
}

function liftSplit(leg: PreparedShipment['legs'][number], piece: LonLat[]): number[][] {
  if (leg.mode !== 'air' || !leg.arc) return piece.map((q) => [q[0], q[1], 0]);
  return piece.map((q) => [q[0], q[1], arcAltAt(leg.arc!, leg.path, q)]);
}
