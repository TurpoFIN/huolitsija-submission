import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type MouseEvent,
} from 'react';
import maplibregl, { type Map as MLMap } from 'maplibre-gl';
import { MapboxOverlay } from '@deck.gl/mapbox';
import { ChevronDown, List, Minus, Plus, RefreshCw, Search, SlidersHorizontal, TriangleAlert, X } from 'lucide-react';
import { connectionView, ShipmentFeed } from './feed';
import { MapRenderer, type FrameStats } from './renderer';
import { readPalette } from './colors';
import { bounds, type LonLat } from './geo';
import type { PreparedShipment } from './prepare';
import type { Locale, Mode, Scope, Shipment, Status, ThemeName, ViewPreset } from './types';
import { fmtDate, fmtInt, fmtRelative, fmtTime, MODES, STATUSES, strings, type Strings } from './i18n';
import {
  applyFilters,
  EMPTY_FILTERS,
  facetCounts,
  isFiltered,
  loadFilters,
  saveFilters,
  sortForList,
  type Filters,
} from './filters';
import { DetailPanel } from './DetailPanel';
import { ShipmentList } from './ShipmentList';
import { IconButton, ModeGlyph, StatusBadge } from './ui';

export interface ShipmentMapProps {
  /** Snapshot endpoint from the map data contract, e.g. "/api/map/shipments". */
  endpoint: string;
  observations?: {id: string; label: string; kind: string; lat: number; lon: number; at: string}[];
  observationFocus?: string;
  /** Hide a duplicate detail card when embedded inside the selected shipment page. */
  showDetailPanel?: boolean;
  /** "customer" (portal) or "ops" (console). Sent as ?scope= and decides the detail link. */
  scope: Scope;
  /** SSE endpoint. Default: endpoint with /shipments → /stream. `null` disables streaming (polling only). */
  streamUrl?: string | null;
  /** Merged into every request, e.g. { credentials: 'include' } or static headers. */
  fetchInit?: RequestInit;
  /** Per-request headers, e.g. a fresh bearer token: () => ({ Authorization: `Bearer ${token}` }). */
  getHeaders?: () => Record<string, string> | Promise<Record<string, string>>;
  /** Detail page URL. Default: /portal/shipments/<id> (customer) or /ops/shipments/<id> (ops). */
  getDetailHref?: (shipment: Shipment) => string;
  /** SPA navigation hook (e.g. react-router navigate). Plain clicks call it; modified clicks open the href. */
  onNavigate?: (href: string, shipment: Shipment, event: MouseEvent<HTMLAnchorElement>) => void;
  /** Called when the selected shipment changes. */
  onSelect?: (shipment: Shipment | null) => void;
  /** Open with this shipment selected (deep link). */
  initialSelectedId?: string;
  /** "inherit" (default) follows <html data-theme> / .dark class, then the OS setting. */
  theme?: ThemeName | 'system' | 'inherit';
  locale?: Locale;
  initialView?: ViewPreset;
  pollIntervalMs?: number;
  /** Reconnect the stream after this long without any bytes (half-open connections). Default 45 s. */
  stallTimeoutMs?: number;
  /** A position older than this is marked stale in the panel. Default 2 h. */
  staleAfterMs?: number;
  /** localStorage key for filters; null disables persistence. Default "huo-map:<scope>". */
  persistKey?: string | null;
  /** Basemap style per theme. Default: OpenFreeMap Positron / Dark (free, no key). */
  mapStyle?: { light: string | Record<string, unknown>; dark: string | Record<string, unknown> };
  /** Show a frame-rate readout (for performance checks). */
  showStats?: boolean;
  className?: string;
  style?: CSSProperties;
}

const DEFAULT_STYLE = {
  light: 'https://tiles.openfreemap.org/styles/positron',
  dark: 'https://tiles.openfreemap.org/styles/dark',
};

const VIEWS: Record<ViewPreset, { bounds?: [[number, number], [number, number]]; center?: [number, number]; zoom?: number }> = {
  world: { center: [48, 38], zoom: 1.9 },
  europe: { bounds: [[-11, 35], [42, 71]] },
  finland: { bounds: [[19.5, 58.7], [31.2, 66.4]] },
};

function useResolvedTheme(theme: ShipmentMapProps['theme']): ThemeName {
  const read = useCallback((): ThemeName => {
    if (theme === 'light' || theme === 'dark') return theme;
    if (typeof document === 'undefined') return 'light';
    if (theme !== 'system') {
      const html = document.documentElement;
      const attr = html.getAttribute('data-theme');
      if (attr === 'dark' || attr === 'light') return attr;
      if (html.classList.contains('dark')) return 'dark';
      if (html.classList.contains('light')) return 'light';
    }
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }, [theme]);
  const [value, setValue] = useState<ThemeName>(read);
  useEffect(() => {
    setValue(read());
    if (theme === 'light' || theme === 'dark') return;
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)');
    const update = () => setValue(read());
    mq?.addEventListener('change', update);
    const mo = new MutationObserver(update);
    if (theme !== 'system') mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
    return () => {
      mq?.removeEventListener('change', update);
      mo.disconnect();
    };
  }, [theme, read]);
  return value;
}

/** Basemap place names in the UI language (OpenMapTiles name:xx), falling back to Latin script. */
function localizeLabels(map: MLMap, locale: Locale): void {
  const field = ['coalesce', ['get', `name:${locale}`], ['get', 'name:en'], ['get', 'name:latin'], ['get', 'name']];
  for (const layer of map.getStyle()?.layers ?? []) {
    if (layer.type !== 'symbol') continue;
    const tf = map.getLayoutProperty(layer.id, 'text-field');
    if (tf && JSON.stringify(tf).includes('name:')) map.setLayoutProperty(layer.id, 'text-field', field as never);
  }
}

// Keep the provider's required attribution and links, translating its connective UI copy.
function localizedStyle(next: any, locale: Locale) {
  const sources = { ...next.sources };
  for (const [id, source] of Object.entries(sources) as [string, any][]) {
    if (source.url?.startsWith('https://tiles.openfreemap.org/')) {
      sources[id] = { ...source, attribution: '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> <a href="https://www.openmaptiles.org/" target="_blank">&copy; OpenMapTiles</a> ' + (locale === 'fi' ? 'Aineisto: ' : 'Data from ') + '<a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>' };
    }
  }
  return { ...next, sources };
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

export function ShipmentMap(props: ShipmentMapProps) {
  const {
    endpoint,
    showDetailPanel = true,
    scope,
    streamUrl,
    fetchInit,
    getHeaders,
    getDetailHref,
    onNavigate,
    onSelect,
    initialSelectedId,
    locale = 'fi',
    initialView = 'world',
    pollIntervalMs = 30000,
    stallTimeoutMs,
    staleAfterMs = 2 * 3600 * 1000,
    showStats = false,
    className,
    style,
  } = props;
  const S: Strings = strings[locale] ?? strings.en;
  const theme = useResolvedTheme(props.theme ?? 'inherit');
  const persistKey = props.persistKey === undefined ? `huo-map:${scope}` : props.persistKey;
  const mapStyle = props.mapStyle ?? DEFAULT_STYLE;

  const rootRef = useRef<HTMLDivElement>(null);
  const mapEl = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const rendererRef = useRef<MapRenderer | null>(null);
  const styleThemeRef = useRef<ThemeName>(theme);

  // ---- data ------------------------------------------------------------------
  const headersRef = useRef(getHeaders);
  headersRef.current = getHeaders;
  const initRef = useRef(fetchInit);
  initRef.current = fetchInit;
  const feed = useMemo(
    () =>
      new ShipmentFeed({
        endpoint,
        scope,
        streamUrl,
        pollIntervalMs,
        stallTimeoutMs,
        fetchInit: initRef.current,
        getHeaders: () => (headersRef.current ? headersRef.current() : {}),
      }),
    [endpoint, scope, streamUrl, pollIntervalMs, stallTimeoutMs],
  );
  useEffect(() => {
    feed.start();
    return () => feed.stop();
  }, [feed]);
  const fs = useSyncExternalStore(feed.subscribe, feed.getState, feed.getState);
  const all = useMemo(() => [...feed.shipments.values()], [feed, fs.version]);

  // ---- filters & selection -----------------------------------------------------
  const [filters, setFilters] = useState<Filters>(() => loadFilters(persistKey));
  useEffect(() => saveFilters(persistKey, filters), [persistKey, filters]);
  const filtered = useMemo(() => applyFilters(all, filters), [all, filters]);
  const listItems = useMemo(() => sortForList(filtered), [filtered]);
  const counts = useMemo(() => facetCounts(all, filters), [all, filters]);
  const customers = useMemo(() => [...new Set(all.map((p) => p.s.customer_name))].sort((a, b) => a.localeCompare(b)), [all]);
  const showCustomer = scope === 'ops' && customers.length > 1;

  const [selectedId, setSelectedId] = useState<string | null>(initialSelectedId ?? null);
  const selected = selectedId ? feed.shipments.get(selectedId) ?? null : null;
  const [hover, setHover] = useState<{ id: string; x: number; y: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => setNow(Date.now()), [fs.tick]);
  useEffect(() => {
    if (!fs.retryAt) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [fs.retryAt]);

  // ---- layout ----------------------------------------------------------------
  const [narrow, setNarrow] = useState(false);
  const [listOpen, setListOpen] = useState(true);
  const [sheet, setSheet] = useState<'none' | 'filters' | 'list'>('none');
  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setNarrow(el.clientWidth < 768));
    ro.observe(el);
    setNarrow(el.clientWidth < 768);
    // The list docks beside the map only when the map keeps most of the width.
    setListOpen(el.clientWidth >= 1100);
    return () => ro.disconnect();
  }, []);

  const padding = useCallback(() => {
    const m = mapRef.current;
    const w = m?.getContainer().clientWidth ?? 1000;
    const h = m?.getContainer().clientHeight ?? 800;
    if (narrow) return { top: 24, left: 24, right: 24, bottom: selectedId && showDetailPanel ? Math.round(h * 0.5) : 56 };
    return { top: 48, left: 48, bottom: 48, right: selectedId && showDetailPanel ? Math.min(400, w * 0.4) : 48 };
  }, [narrow, selectedId, showDetailPanel]);

  const goTo = useCallback(
    (view: ViewPreset) => {
      const m = mapRef.current;
      if (!m) return;
      const v = VIEWS[view];
      const duration = prefersReducedMotion() ? 0 : 2600;
      if (v.bounds) m.fitBounds(v.bounds, { padding: padding(), duration, essential: true, curve: 1.42, linear: false });
      else m.flyTo({ center: v.center!, zoom: narrow ? (v.zoom ?? 1.4) - 0.6 : v.zoom, duration, essential: true, curve: 1.42 });
    },
    [padding, narrow, showDetailPanel],
  );

  const focusShipment = useCallback(
    (p: PreparedShipment) => {
      const m = mapRef.current;
      if (!m) return;
      const pts: LonLat[] = [];
      for (const l of p.legs) for (let i = 0; i < l.path.length; i += Math.max(1, Math.floor(l.path.length / 40))) pts.push(l.path[i]);
      pts.push([p.s.position.lon, p.s.position.lat], [p.s.origin.lon, p.s.origin.lat], [p.s.destination.lon, p.s.destination.lat]);
      const b = bounds(pts);
      const duration = prefersReducedMotion() ? 0 : 1400;
      const pad = padding();
      if (narrow && showDetailPanel) pad.bottom = Math.round((m.getContainer().clientHeight ?? 700) * 0.55);
      else pad.right = showDetailPanel ? 400 : 36;
      m.fitBounds(b, { padding: pad, maxZoom: 7, duration, essential: true });
    },
    [padding, narrow, showDetailPanel],
  );

  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const select = useCallback(
    (id: string | null, opts?: { fly?: boolean }) => {
      const prev = selectedIdRef.current;
      if (!id && prev) {
        // Return focus to where the selection came from: its list row, else the map.
        requestAnimationFrame(() => {
          const row = rootRef.current?.querySelector<HTMLElement>(`.huo-row[data-id="${CSS.escape(prev)}"]`);
          (row ?? mapRef.current?.getCanvas())?.focus({ preventScroll: true });
        });
      }
      setSelectedId(id);
      const p = id ? feed.shipments.get(id) : null;
      onSelect?.(p ? p.s : null);
      if (p && opts?.fly) focusShipment(p);
      if (narrow && id) setSheet('none');
    },
    [feed, onSelect, focusShipment, narrow],
  );

  // ---- map lifecycle -----------------------------------------------------------
  const [mapReady, setMapReady] = useState(0);
  const localeRef = useRef(locale);
  localeRef.current = locale;
  const selectRef = useRef(select);
  selectRef.current = select;
  useEffect(() => {
    const el = mapEl.current;
    if (!el) return;
    const v = VIEWS[initialView];
    const map = new maplibregl.Map({
      container: el,
      style: undefined,
      center: v.center ?? [25, 60],
      zoom: v.zoom ?? 1.4,
      attributionControl: { compact: true },
      locale: localeRef.current === "fi" ? { "AttributionControl.ToggleAttribution": "Näytä kartan lähteet", "Map.Title": "Kartta" } : {},
      dragRotate: false,
      pitchWithRotate: false,
      maxPitch: 0,
      fadeDuration: 0,
      cooperativeGestures: false,
    });
    map.touchZoomRotate.disableRotation();
    mapRef.current = map;
    const overlay = new MapboxOverlay({ interleaved: true, layers: [] });
    let renderer: MapRenderer | null = null;

    let disposed = false;
    const onStyle = () => {
      // React StrictMode / fast unmounts: a removed map can still finish loading its style.
      if (disposed || !rootRef.current) return;
      map.setProjection({ type: 'globe' });
      localizeLabels(map, localeRef.current);
      const root = rootRef.current!;
      const pal = readPalette(root, styleThemeRef.current === 'dark');
      const canvas = `rgb(${pal.canvas[0]}, ${pal.canvas[1]}, ${pal.canvas[2]})`;
      map.setSky({
        'sky-color': canvas,
        'horizon-color': canvas,
        'fog-color': canvas,
        'sky-horizon-blend': 0.5,
        'atmosphere-blend': ['interpolate', ['linear'], ['zoom'], 0, 0.6, 5, 0.3, 7, 0],
      } as never);
      if (!renderer) {
        map.addControl(overlay);
        renderer = new MapRenderer(map, overlay, feed, pal, {
          onHover: (id, x, y) => setHover(id ? { id, x, y } : null),
          onClick: (id) => selectRef.current(id),
        });
        rendererRef.current = renderer;
      } else {
        renderer.findLabelLayer();
        renderer.setPalette(pal);
      }
      setMapReady((n) => n + 1);
    };
    map.on('style.load', onStyle);
    // Some basemap styles reference sprite icons they don't ship (OpenFreeMap Dark: circle-11).
    // Register an empty image so MapLibre doesn't log a missing-image warning per tile.
    map.on('styleimagemissing', (e: { id: string }) => {
      if (!map.hasImage(e.id)) map.addImage(e.id, { width: 1, height: 1, data: new Uint8Array(4) });
    });
    map.setStyle(mapStyle[styleThemeRef.current] as never, { transformStyle: (_previous, next) => localizedStyle(next, localeRef.current) });
    if (!v.center && v.bounds) map.once('load', () => map.fitBounds(v.bounds!, { duration: 0, padding: 32 }));
    return () => {
      disposed = true;
      map.off('style.load', onStyle);
      renderer?.destroy();
      rendererRef.current = null;
      map.remove();
      mapRef.current = null;
    };
    // The map is created once per feed; theme and selection are pushed in below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [feed]);

  useEffect(() => {
    const m = mapRef.current;
    if (m && m.isStyleLoaded()) localizeLabels(m, locale);
  }, [locale, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    const data: GeoJSON.FeatureCollection = {type: 'FeatureCollection', features: (props.observations || []).map(o => ({
      type: 'Feature', geometry: {type: 'Point', coordinates: [o.lon, o.lat]}, properties: {label: o.label, kind: o.kind}
    }))};
    const source = map.getSource('visibility-observations') as maplibregl.GeoJSONSource | undefined;
    if (source) source.setData(data);
    else {
      map.addSource('visibility-observations', {type: 'geojson', data});
      map.addLayer({id: 'visibility-observations-points', type: 'circle', source: 'visibility-observations', paint: {
        'circle-radius': 6, 'circle-color': ['match', ['get', 'kind'], 'flight', '#9b4dab', '#147e8b'],
        'circle-stroke-width': 2, 'circle-stroke-color': '#ffffff'
      }});
      map.addLayer({id: 'visibility-observations-labels', type: 'symbol', source: 'visibility-observations', layout: {
        'text-field': ['get', 'label'], 'text-size': 12, 'text-offset': [0, 1.2], 'text-anchor': 'top'
      }, paint: {'text-color': '#172126', 'text-halo-color': '#ffffff', 'text-halo-width': 2}});
    }
  }, [props.observations, mapReady, theme]);
  useEffect(() => {
    const o = props.observations?.find(x => x.id === props.observationFocus);
    if (o && mapReady) mapRef.current?.flyTo({center: [o.lon, o.lat], zoom: 5, duration: 500});
  }, [props.observationFocus, mapReady]);

  // Theme: swap the basemap style; palette is re-read on style.load.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || styleThemeRef.current === theme) return;
    styleThemeRef.current = theme;
    map.setStyle(mapStyle[theme] as never, { diff: false, transformStyle: (_previous, next) => localizedStyle(next, localeRef.current) });
  }, [theme, mapStyle]);

  useEffect(() => {
    rendererRef.current?.setVisible(filtered);
  }, [filtered, mapReady]);
  useEffect(() => {
    rendererRef.current?.setSelected(selectedId);
  }, [selectedId, mapReady, fs.version]);
  useEffect(() => {
    rendererRef.current?.setHovered(hover?.id ?? null);
  }, [hover?.id]);
  useEffect(() => {
    // Resize the GL canvas when panels open/close.
    const t = setTimeout(() => mapRef.current?.resize(), 0);
    return () => clearTimeout(t);
  }, [listOpen, narrow, selectedId]);
  useEffect(() => {
    if (initialSelectedId && fs.count && feed.shipments.has(initialSelectedId) && mapReady) {
      const p = feed.shipments.get(initialSelectedId);
      if (p) focusShipment(p);
    }
    // only once data and map are ready
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapReady, fs.count > 0]);

  // Escape closes the panel or sheet.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (sheet !== 'none') setSheet('none');
      else if (selectedId) select(null);
    };
    const el = rootRef.current;
    el?.addEventListener('keydown', onKey);
    return () => el?.removeEventListener('keydown', onKey);
  }, [sheet, selectedId, select]);

  // ---- stats (performance evidence) ----------------------------------------------
  const [stats, setStats] = useState<FrameStats | null>(null);
  useEffect(() => {
    if (!showStats) return;
    const t = setInterval(() => {
      const s = rendererRef.current?.stats() ?? null;
      setStats(s);
      (window as unknown as { __huoMapStats?: FrameStats }).__huoMapStats = s ?? undefined;
    }, 1000);
    return () => clearInterval(t);
  }, [showStats]);
  useEffect(() => {
    (window as unknown as { __huoMap?: unknown }).__huoMap = {
      goTo,
      select: (id: string) => select(id, { fly: true }),
      stats: () => rendererRef.current?.stats(),
      map: () => mapRef.current,
    };
  }, [goTo, select]);

  // ---- derived UI --------------------------------------------------------------
  const detailHref = (s: Shipment) =>
    getDetailHref ? getDetailHref(s) : `/${scope === 'ops' ? 'ops' : 'portal'}/shipments/${encodeURIComponent(s.id)}`;
  const hasData = fs.count > 0;
  const filteredOn = isFiltered(filters);
  const attentionTotal = counts.attention;
  const setF = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }));
  const toggleMode = (m: Mode) =>
    setF({ modes: filters.modes.includes(m) ? filters.modes.filter((x) => x !== m) : [...filters.modes, m] });
  const hovered = hover ? feed.shipments.get(hover.id) : null;

  const view = connectionView(fs);
  const retryIn = fs.retryAt ? Math.max(0, Math.ceil((fs.retryAt - now) / 1000)) : 0;
  const connection = (
    <div className="huo-conn" data-state={view} aria-live="polite">
      <span className="huo-conn__dot" aria-hidden />
      <span>
        {view === 'live'
          ? S.live
          : view === 'polling'
            ? S.polling(Math.round(pollIntervalMs / 1000))
            : view === 'connecting'
              ? S.connecting
              : view === 'degraded'
                ? S.liveRefreshFailed
                : `${view === 'failed' ? S.loadFailedShort : S.offline} · ${S.retryingIn(retryIn)}`}
      </span>
      {fs.hasSnapshot && fs.lastDataAt && (
        <span className="huo-muted huo-num">· {S.updated(fmtRelative(fs.lastDataAt, now, locale))}</span>
      )}
      {(view === 'failed' || view === 'offline' || view === 'degraded') && (
        <button type="button" className="huo-link-btn" onClick={() => feed.retry()}>
          <RefreshCw size={12} aria-hidden /> {S.retry}
        </button>
      )}
    </div>
  );

  const modeChips = (
    <div className="huo-chips" role="group" aria-label={S.filters}>
      {MODES.map((m) => (
        <button
          key={m}
          type="button"
          className="huo-chip"
          aria-pressed={filters.modes.includes(m)}
          onClick={() => toggleMode(m)}
          data-mode={m}
        >
          <ModeGlyph mode={m} size={13} />
          {S.mode[m]}
          <span className="huo-chip__count huo-num">{fmtInt(counts.mode[m], locale)}</span>
        </button>
      ))}
    </div>
  );

  const statusMenu = (
    <StatusMenu
      S={S}
      locale={locale}
      selected={filters.statuses}
      counts={counts.status}
      onChange={(statuses) => setF({ statuses })}
    />
  );

  const customerSelect = showCustomer ? (
    <label className="huo-select">
      <span className="huo-sr">{S.customer}</span>
      <select value={filters.customer} onChange={(e) => setF({ customer: e.target.value })}>
        <option value="">{S.allCustomers}</option>
        {customers.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
      <ChevronDown size={14} aria-hidden />
    </label>
  ) : null;

  const attentionChip = (
    <button
      type="button"
      className="huo-attention"
      aria-pressed={filters.attention}
      data-empty={attentionTotal === 0 || undefined}
      disabled={attentionTotal === 0 && !filters.attention}
      onClick={() => setF({ attention: !filters.attention })}
      title={S.attentionCount(counts.attention, counts.exceptions, counts.holds, counts.both)}
    >
      <TriangleAlert size={14} aria-hidden />
      {narrow
        ? S.attentionShort(counts.attention)
        : S.attentionCount(counts.attention, counts.exceptions, counts.holds, counts.both)}
    </button>
  );

  const search = (
    <label className="huo-search">
      <Search size={14} aria-hidden />
      <span className="huo-sr">{S.search}</span>
      <input
        type="search"
        value={filters.q}
        placeholder={S.search}
        onChange={(e) => setF({ q: e.target.value })}
        autoComplete="off"
        spellCheck={false}
      />
    </label>
  );

  const countLabel = <span className="huo-count huo-num">{S.shown(filtered.length, fs.count)}</span>;

  const list = (
    <ShipmentList
      items={listItems}
      S={S}
      locale={locale}
      selectedId={selectedId}
      showCustomer={showCustomer}
      onSelect={(id) => select(id, { fly: true })}
      onHover={(id) => rendererRef.current?.setHovered(id)}
      label={S.list}
    />
  );

  return (
    <div
      ref={rootRef}
      className={`huo-map ${className ?? ''}`}
      data-theme={theme}
      data-layout={narrow ? 'narrow' : 'wide'}
      data-list={!narrow && listOpen ? 'open' : 'closed'}
      data-panel={selected && showDetailPanel ? 'open' : 'closed'}
      style={style}
    >
      {/* Toolbar */}
      {narrow ? (
        <header className="huo-bar huo-bar--narrow">
          <div className="huo-bar__row">
            {search}
            <IconButton label={S.filters} onClick={() => setSheet(sheet === 'filters' ? 'none' : 'filters')} pressed={sheet === 'filters'}>
              <SlidersHorizontal size={16} aria-hidden />
              {(filters.modes.length + filters.statuses.length + (filters.customer ? 1 : 0)) > 0 && <span className="huo-dot-badge" />}
            </IconButton>
            <IconButton label={S.list} onClick={() => setSheet(sheet === 'list' ? 'none' : 'list')} pressed={sheet === 'list'}>
              <List size={16} aria-hidden />
            </IconButton>
          </div>
          <div className="huo-bar__row huo-bar__row--meta">
            {attentionChip}
            {countLabel}
          </div>
        </header>
      ) : (
        <header className="huo-bar">
          <div className="huo-bar__row">
            <IconButton label={listOpen ? S.hideList : S.list} onClick={() => setListOpen((o) => !o)} pressed={listOpen}>
              <List size={16} aria-hidden />
            </IconButton>
            {search}
            {modeChips}
            {statusMenu}
            {customerSelect}
            {filteredOn && (
              <button type="button" className="huo-link-btn" onClick={() => setFilters(EMPTY_FILTERS)}>
                {S.clearFilters}
              </button>
            )}
          </div>
          <div className="huo-bar__row huo-bar__row--meta">
            {attentionChip}
            {countLabel}
            <span className="huo-spacer" />
            {connection}
          </div>
        </header>
      )}

      <div className="huo-viewbar">            <div className="huo-seg" role="group" aria-label={locale === 'fi' ? 'Karttanäkymä' : 'Map view'}>
              {(['world', 'europe', 'finland'] as ViewPreset[]).map((v) => (
                <button key={v} type="button" onClick={() => goTo(v)}>
                  {S[v]}
                </button>
              ))}
            </div>
</div>
      <div className="huo-body">
        {!narrow && listOpen && <nav className="huo-list" aria-label={S.list}>{list}</nav>}

        <div className="huo-stage">
          <div ref={mapEl} className="huo-canvas" role="application" aria-label={S.mapLabel} />

          {/* Camera controls */}
          <div className="huo-controls">
            <div className="huo-zoom">
              <IconButton label={S.zoomIn} onClick={() => mapRef.current?.zoomIn()}>
                <Plus size={14} aria-hidden />
              </IconButton>
              <IconButton label={S.zoomOut} onClick={() => mapRef.current?.zoomOut()}>
                <Minus size={14} aria-hidden />
              </IconButton>
            </div>
          </div>

          <Legend S={S} narrow={narrow} />

          {narrow && <div className="huo-conn-float">{connection}</div>}

          {showStats && stats && (
            <div className="huo-stats huo-num" aria-hidden>
              {stats.fps} {S.fps} · p95 {stats.frameMsP95} ms · {fmtInt(stats.markers, locale)} markers · {stats.lanes} lanes
            </div>
          )}

          {/* Hover card */}
          {hovered && hover && !narrow && hover.id !== selectedId && (
            <div className="huo-tip" style={{ left: hover.x + 14, top: hover.y + 14 }} role="tooltip">
              <div className="huo-tip__top">
                <span className="huo-mono">{hovered.s.reference}</span>
                <StatusBadge status={hovered.s.status} S={S} />
              </div>
              <div className="huo-truncate">
                {hovered.s.origin.name} → {hovered.s.destination.name}
              </div>
              <div className="huo-muted huo-num">
                {S.eta} {fmtDate(hovered.s.eta, locale)}
                {showCustomer ? ` · ${hovered.s.customer_name}` : ''}
              </div>
            </div>
          )}

          {/* States */}
          {!fs.hasSnapshot && !fs.error && (
            <div className="huo-state" role="status">
              <span className="huo-spinner" aria-hidden />
              {S.loading}
            </div>
          )}
          {!fs.hasSnapshot && fs.error && (
            <div className="huo-state" role="alert" data-state="failed">
              <strong>{S.loadFailed}</strong>
              <span className="huo-muted huo-small">
                {fs.error} · {S.retryingIn(retryIn)}
              </span>
              <button type="button" className="huo-btn" onClick={() => feed.retry()}>
                <RefreshCw size={14} aria-hidden />
                {S.retry}
              </button>
            </div>
          )}
          {fs.hasSnapshot && !hasData && !fs.error && (
            <div className="huo-state" role="status">
              <strong>{S.emptyNone}</strong>
              <span className="huo-muted huo-small">{S.emptyNoneHint}</span>
            </div>
          )}
          {hasData && filtered.length === 0 && (
            <div className="huo-state" role="status">
              <strong>{S.emptyFiltered}</strong>
              <button type="button" className="huo-btn" onClick={() => setFilters(EMPTY_FILTERS)}>
                {S.clearFilters}
              </button>
            </div>
          )}
          {fs.hasSnapshot && fs.error && (
            <div className="huo-banner" role="alert">
              <TriangleAlert size={14} aria-hidden />
              <span>{S.refreshFailed(fs.lastDataAt ? fmtTime(fs.lastDataAt, locale) : '')}</span>
              <button type="button" className="huo-link-btn" onClick={() => feed.retry()}>
                {S.retry}
              </button>
            </div>
          )}

          {selected && showDetailPanel && (
            <DetailPanel
              p={selected}
              S={S}
              locale={locale}
              now={now}
              staleAfterMs={staleAfterMs}
              showCustomer={showCustomer}
              href={detailHref(selected.s)}
              onNavigate={onNavigate}
              onClose={() => select(null)}
            />
          )}

          {narrow && sheet !== 'none' && (
            <div className="huo-sheet" role="dialog" aria-modal="false" aria-label={sheet === 'filters' ? S.filters : S.list}>
              <div className="huo-sheet__head">
                <strong>{sheet === 'filters' ? S.filters : S.shown(filtered.length, fs.count)}</strong>
                <IconButton label={S.close} onClick={() => setSheet('none')}>
                  <X size={14} aria-hidden />
                </IconButton>
              </div>
              {sheet === 'filters' ? (
                <div className="huo-sheet__body">
                  {modeChips}
                  {statusMenuInline(S, locale, filters, counts.status, (statuses) => setF({ statuses }))}
                  {customerSelect}
                  {filteredOn && (
                    <button type="button" className="huo-btn" onClick={() => setFilters(EMPTY_FILTERS)}>
                      {S.clearFilters}
                    </button>
                  )}
                </div>
              ) : (
                <div className="huo-sheet__list">{list}</div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Legend({ S, narrow }: { S: Strings; narrow: boolean }) {
  const [open, setOpen] = useState(!narrow);
  useEffect(() => setOpen(!narrow), [narrow]);
  return (
    <div className="huo-legend" data-open={open}>
      <button type="button" className="huo-legend__toggle" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {S.legend}
        <ChevronDown size={12} aria-hidden />
      </button>
      {open && (
        <ul>
          {(['sea', 'road', 'rail', 'air'] as const).map((m) => (
            <li key={m}>
              <span className="huo-legend__line" data-mode={m} aria-hidden />
              {S.mode[m]}
            </li>
          ))}
          <li>
            <span className="huo-legend__ring" data-tone="danger" aria-hidden />
            {S.status.exception}
          </li>
          <li>
            <span className="huo-legend__ring" data-tone="warning" aria-hidden />
            {S.status.customs_hold}
          </li>
        </ul>
      )}
    </div>
  );
}

function statusMenuInline(
  S: Strings,
  locale: Locale,
  f: Filters,
  counts: Record<Status, number>,
  onChange: (s: Status[]) => void,
) {
  return (
    <fieldset className="huo-checks">
      <legend className="huo-label">{S.allStatuses}</legend>
      {STATUSES.map((st) => (
        <label key={st} className="huo-check">
          <input
            type="checkbox"
            checked={f.statuses.includes(st)}
            onChange={() => onChange(f.statuses.includes(st) ? f.statuses.filter((x) => x !== st) : [...f.statuses, st])}
          />
          <StatusBadge status={st} S={S} />
          <span className="huo-muted huo-num">{fmtInt(counts[st] ?? 0, locale)}</span>
        </label>
      ))}
    </fieldset>
  );
}

function StatusMenu({
  S,
  locale,
  selected,
  counts,
  onChange,
}: {
  S: Strings;
  locale: Locale;
  selected: Status[];
  counts: Record<Status, number>;
  onChange: (s: Status[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: PointerEvent) => {
      if (!pop.current?.contains(e.target as Node) && !btn.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        setOpen(false);
        btn.current?.focus();
      }
    };
    document.addEventListener('pointerdown', onDoc);
    document.addEventListener('keydown', onKey, true);
    (pop.current?.querySelector('input') as HTMLInputElement | null)?.focus();
    return () => {
      document.removeEventListener('pointerdown', onDoc);
      document.removeEventListener('keydown', onKey, true);
    };
  }, [open]);
  const label = selected.length === 0 ? S.allStatuses : selected.length === 1 ? S.status[selected[0]] : S.statuses(selected.length);
  return (
    <div className="huo-menu">
      <button
        ref={btn}
        type="button"
        className="huo-chip huo-chip--menu"
        aria-expanded={open}
        aria-haspopup="true"
        aria-pressed={selected.length > 0}
        onClick={() => setOpen((o) => !o)}
      >
        {label}
        <ChevronDown size={14} aria-hidden />
      </button>
      {open && (
        <div ref={pop} className="huo-popover">
          {STATUSES.map((st) => (
            <label key={st} className="huo-check">
              <input
                type="checkbox"
                checked={selected.includes(st)}
                onChange={() => onChange(selected.includes(st) ? selected.filter((x) => x !== st) : [...selected, st])}
              />
              <StatusBadge status={st} S={S} />
              <span className="huo-muted huo-num">{fmtInt(counts[st] ?? 0, locale)}</span>
            </label>
          ))}
          {selected.length > 0 && (
            <button type="button" className="huo-link-btn" onClick={() => onChange([])}>
              {S.allStatuses}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
