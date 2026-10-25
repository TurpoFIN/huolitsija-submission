// Live shipment feed: one snapshot (GET /api/map/shipments), then Server-Sent Events
// (GET /api/map/stream, event "position"). If the stream cannot be opened or drops,
// the feed polls the snapshot every `pollIntervalMs` until the stream is back.
//
// SSE is read with fetch() + a stream reader instead of EventSource so the same
// credentials and headers (e.g. Authorization: Bearer …) apply to both requests.

import { isAttention, legFromProgress, prepare, type PreparedShipment } from './prepare';
import type { Connection, PositionEvent, Scope, Shipment, ShipmentsResponse } from './types';

export interface FeedOptions {
  /** Snapshot endpoint, e.g. "/api/map/shipments". `scope` is added as a query parameter. */
  endpoint: string;
  /** SSE endpoint. Defaults to the endpoint with "/shipments" replaced by "/stream". */
  streamUrl?: string | null;
  scope: Scope;
  /** Merged into every request (credentials, static headers). */
  fetchInit?: RequestInit;
  /** Called before every request; use it for short-lived tokens. */
  getHeaders?: () => Record<string, string> | Promise<Record<string, string>>;
  pollIntervalMs?: number;
  /** Full snapshot refresh while live, to pick up new shipments and leg changes. */
  resyncIntervalMs?: number;
  /**
   * Reconnect when the stream delivers no bytes (events or `: ping` comments) for this long.
   * Catches half-open connections after network loss. Default 45 s (3x a 15 s ping).
   */
  stallTimeoutMs?: number;
}

export interface FeedState {
  connection: Connection;
  /** True until the first snapshot succeeds or fails. */
  loading: boolean;
  /** True once any snapshot has loaded. Until then there is no data to show, whatever the stream does. */
  hasSnapshot: boolean;
  /** Last snapshot error. Cleared only by a successful snapshot (never by the stream opening). */
  error: string | null;
  /** When the next automatic snapshot retry runs after a failure (wall-clock ms), else null. */
  retryAt: number | null;
  /** Wall-clock ms of the last data received (snapshot or event). */
  lastDataAt: number | null;
  generatedAt: string | null;
  /** Bumped when shipments are added/removed or a status/leg changes (throttled for UI). */
  version: number;
  /** Bumped on any data change, including position-only updates (throttled for UI). */
  tick: number;
  count: number;
}

export interface PositionChange {
  p: PreparedShipment;
  prevLon: number;
  prevLat: number;
}

type Listener = () => void;

export function withScope(endpoint: string, scope: Scope): string {
  const base = typeof window !== 'undefined' ? window.location.href : 'http://localhost/';
  const u = new URL(endpoint, base);
  if (!u.searchParams.has('scope')) u.searchParams.set('scope', scope);
  return u.toString();
}

export function defaultStreamUrl(endpoint: string): string {
  const base = typeof window !== 'undefined' ? window.location.href : 'http://localhost/';
  const u = new URL(endpoint, base);
  u.pathname = u.pathname.replace(/\/shipments\/?$/, '/stream');
  return u.toString();
}

function validShipment(s: unknown): s is Shipment {
  const x = s as Shipment;
  return (
    !!x &&
    typeof x.id === 'string' &&
    !!x.position &&
    Number.isFinite(x.position.lat) &&
    Number.isFinite(x.position.lon) &&
    Array.isArray(x.legs)
  );
}

export type ConnectionView = 'connecting' | 'live' | 'polling' | 'failed' | 'offline' | 'degraded';

/**
 * What the connection indicator shows. "live" needs a loaded snapshot and no snapshot error:
 * an open stream alone never reads as live.
 * - connecting: first snapshot pending
 * - failed: no snapshot has ever loaded and the last attempt failed (retrying with backoff)
 * - degraded: data loaded and stream live, but the latest snapshot refresh failed
 * - offline: data loaded, no stream, latest refresh failed
 */
export function connectionView(s: FeedState): ConnectionView {
  if (!s.hasSnapshot) return s.error ? 'failed' : 'connecting';
  if (s.error) return s.connection === 'live' ? 'degraded' : 'offline';
  if (s.connection === 'live') return 'live';
  if (s.connection === 'connecting') return 'connecting';
  return s.connection === 'offline' ? 'offline' : 'polling';
}

export class ShipmentFeed {
  readonly shipments = new Map<string, PreparedShipment>();
  state: FeedState = {
    connection: 'connecting',
    loading: true,
    hasSnapshot: false,
    error: null,
    retryAt: null,
    lastDataAt: null,
    generatedAt: null,
    version: 0,
    tick: 0,
    count: 0,
  };
  /** Shipments the server returned that did not match the contract, with the reason. */
  rejected: { id?: string; reason: string }[] = [];

  private opts: Required<Omit<FeedOptions, 'getHeaders' | 'fetchInit' | 'streamUrl'>> &
    Pick<FeedOptions, 'getHeaders' | 'fetchInit'> & { streamUrl: string | null };
  private uiListeners = new Set<Listener>();
  private posListeners = new Set<(changes: PositionChange[]) => void>();
  private setListeners = new Set<Listener>();
  private stopped = true;
  private streamAbort: AbortController | null = null;
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private resyncTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private refreshSoonTimer: ReturnType<typeof setTimeout> | null = null;
  private uiTimer: ReturnType<typeof setTimeout> | null = null;
  private lastUiEmit = 0;
  private reconnectDelay = 5000;
  private serverRetry = 5000;
  private pending: PositionChange[] = [];
  private flushScheduled = false;
  private snapshotInFlight: Promise<void> | null = null;
  private failures = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private structuralPending = false;
  private lastData: number | null = null;

  constructor(options: FeedOptions) {
    this.opts = {
      endpoint: withScope(options.endpoint, options.scope),
      streamUrl:
        options.streamUrl === null
          ? null
          : withScope(options.streamUrl ?? defaultStreamUrl(options.endpoint), options.scope),
      scope: options.scope,
      fetchInit: options.fetchInit,
      getHeaders: options.getHeaders,
      pollIntervalMs: options.pollIntervalMs ?? 30000,
      resyncIntervalMs: options.resyncIntervalMs ?? 300000,
      stallTimeoutMs: options.stallTimeoutMs ?? 45000,
    };
  }

  // ---- subscriptions -------------------------------------------------------

  /** UI subscription (React). Notified at most once per second. */
  subscribe = (fn: Listener): (() => void) => {
    this.uiListeners.add(fn);
    return () => this.uiListeners.delete(fn);
  };
  getState = (): FeedState => this.state;

  /** Immediate, batched per animation frame: for the animator. */
  onPositions(fn: (changes: PositionChange[]) => void): () => void {
    this.posListeners.add(fn);
    return () => this.posListeners.delete(fn);
  }
  /** Shipments added/removed or replaced by a snapshot. */
  onSetChange(fn: Listener): () => void {
    this.setListeners.add(fn);
    return () => this.setListeners.delete(fn);
  }

  // ---- lifecycle -----------------------------------------------------------

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    void this.loadSnapshot().then(() => {
      if (this.stopped) return;
      if (this.opts.streamUrl) this.openStream();
      else this.schedulePoll();
    });
    this.resyncTimer = setInterval(() => {
      if (this.state.connection === 'live') void this.loadSnapshot();
    }, this.opts.resyncIntervalMs);
  }

  stop(): void {
    this.stopped = true;
    this.streamAbort?.abort();
    this.streamAbort = null;
    for (const t of [this.pollTimer, this.reconnectTimer, this.refreshSoonTimer, this.uiTimer, this.retryTimer]) if (t) clearTimeout(t);
    this.retryTimer = null;
    if (this.resyncTimer) clearInterval(this.resyncTimer);
    this.pollTimer = this.reconnectTimer = this.refreshSoonTimer = this.uiTimer = null;
    this.resyncTimer = null;
  }

  /** Manual retry: refetch now (ignoring backoff) and try the stream again. */
  retry(): void {
    if (this.stopped) return;
    void this.loadSnapshot(true);
    if (this.opts.streamUrl && this.state.connection !== 'live') {
      if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
      this.reconnectDelay = this.serverRetry;
      this.openStream();
    }
  }

  // ---- requests ------------------------------------------------------------

  private async init(extra?: Record<string, string>): Promise<RequestInit> {
    const base = this.opts.fetchInit ?? {};
    const dyn = this.opts.getHeaders ? await this.opts.getHeaders() : {};
    const headers = new Headers(base.headers ?? {});
    for (const [k, v] of Object.entries({ ...dyn, ...(extra ?? {}) })) headers.set(k, v);
    return { ...base, headers };
  }

  /**
   * Fetch the snapshot. After a failure, automatic callers (polling, resync, unknown-id
   * refresh) wait for the backoff (2 s, 4 s, 8 s … 60 s); `force` (manual Retry) does not.
   */
  loadSnapshot(force = false): Promise<void> {
    if (this.snapshotInFlight) return this.snapshotInFlight;
    if (!force && this.state.retryAt && Date.now() < this.state.retryAt) return Promise.resolve();
    const run = async () => {
      try {
        const res = await fetch(this.opts.endpoint, await this.init({ Accept: 'application/json' }));
        if (!res.ok) throw new Error(`HTTP ${res.status}${res.statusText ? ' ' + res.statusText : ''}`);
        const body = (await res.json()) as ShipmentsResponse;
        if (!body || !Array.isArray(body.shipments)) throw new Error('Response has no shipments array');
        this.applySnapshot(body);
        this.failures = 0;
        if (this.retryTimer) clearTimeout(this.retryTimer);
        this.retryTimer = null;
        this.patch({
          loading: false,
          hasSnapshot: true,
          error: null,
          retryAt: null,
          lastDataAt: Date.now(),
          generatedAt: body.generated_at ?? null,
          connection:
            this.state.connection === 'live'
              ? 'live'
              : this.state.connection === 'connecting' && this.opts.streamUrl
                ? 'connecting'
                : 'polling',
        });
      } catch (err) {
        this.failures++;
        const delay = Math.min(60000, 2000 * 2 ** (this.failures - 1));
        const retryAt = Date.now() + delay;
        if (this.retryTimer) clearTimeout(this.retryTimer);
        this.retryTimer = setTimeout(() => {
          this.retryTimer = null;
          if (!this.stopped) void this.loadSnapshot();
        }, delay);
        this.patch({
          loading: false,
          error: err instanceof Error ? err.message : String(err),
          retryAt,
          connection: this.state.connection === 'live' ? 'live' : 'offline',
        });
      } finally {
        this.snapshotInFlight = null;
      }
    };
    this.snapshotInFlight = run();
    return this.snapshotInFlight;
  }

  private applySnapshot(body: ShipmentsResponse): void {
    const seen = new Set<string>();
    const changes: PositionChange[] = [];
    let setChanged = false;
    this.rejected = [];
    for (const raw of body.shipments) {
      if (!validShipment(raw)) {
        this.rejected.push({ id: (raw as Shipment | null)?.id, reason: 'missing id, legs or numeric position' });
        continue;
      }
      seen.add(raw.id);
      const prev = this.shipments.get(raw.id);
      const next = prepare(raw);
      this.shipments.set(raw.id, next);
      if (!prev) setChanged = true;
      else {
        if (prev.s.position.lat !== raw.position.lat || prev.s.position.lon !== raw.position.lon)
          changes.push({ p: next, prevLon: prev.s.position.lon, prevLat: prev.s.position.lat });
        if (prev.s.status !== raw.status || prev.currentLeg !== next.currentLeg || prev.legs !== next.legs) setChanged = true;
      }
    }
    for (const id of [...this.shipments.keys()]) {
      if (!seen.has(id)) {
        this.shipments.delete(id);
        setChanged = true;
      }
    }
    this.state = { ...this.state, count: this.shipments.size };
    if (setChanged || this.shipments.size === 0) this.setListeners.forEach((f) => f());
    if (changes.length) this.posListeners.forEach((f) => f(changes));
    this.structuralPending = true;
    this.bump(true);
  }

  private applyEvent(ev: PositionEvent): void {
    const p = this.shipments.get(ev.id);
    if (!p) {
      // A shipment we have not seen: pick it up with a snapshot shortly.
      this.refreshSoon();
      return;
    }
    if (!ev.position || !Number.isFinite(ev.position.lat) || !Number.isFinite(ev.position.lon)) return;
    const prevLon = p.s.position.lon, prevLat = p.s.position.lat;
    const prevStatus = p.s.status, prevLeg = p.currentLeg;
    p.s = { ...p.s, position: ev.position, progress: ev.progress ?? p.s.progress, status: ev.status ?? p.s.status };
    p.currentLeg = legFromProgress(p.legs, p.s.progress, p.currentLeg);
    p.attention = isAttention(p.s.status, p.s.has_exception);
    this.pending.push({ p, prevLon, prevLat });
    if (prevStatus !== p.s.status || prevLeg !== p.currentLeg) {
      // Status/leg changes re-filter the list; leg timestamps catch up on the next resync.
      this.structuralPending = true;
      this.setListeners.forEach((f) => f());
    }
    if (!this.flushScheduled) {
      this.flushScheduled = true;
      const flush = () => {
        this.flushScheduled = false;
        const batch = this.pending;
        this.pending = [];
        if (batch.length) this.posListeners.forEach((f) => f(batch));
      };
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(flush);
      else setTimeout(flush, 16);
    }
    this.lastData = Date.now();
    this.bump(false);
  }

  private refreshSoon(): void {
    // While snapshots are failing the backoff timer owns the next attempt.
    if (this.refreshSoonTimer || this.failures > 0) return;
    this.refreshSoonTimer = setTimeout(() => {
      this.refreshSoonTimer = null;
      void this.loadSnapshot();
    }, 2000);
  }

  // ---- stream --------------------------------------------------------------

  private openStream(): void {
    if (this.stopped || !this.opts.streamUrl) return;
    this.streamAbort?.abort();
    const ctrl = new AbortController();
    this.streamAbort = ctrl;
    let stalled = false;
    let lastByte = Date.now();
    const watchdog = setInterval(() => {
      if (Date.now() - lastByte > this.opts.stallTimeoutMs) {
        stalled = true;
        ctrl.abort();
      }
    }, Math.min(5000, Math.max(50, this.opts.stallTimeoutMs / 4)));
    void (async () => {
      try {
        const init = await this.init({ Accept: 'text/event-stream', 'Cache-Control': 'no-cache' });
        const res = await fetch(this.opts.streamUrl!, { ...init, signal: ctrl.signal });
        const type = res.headers.get('content-type') ?? '';
        if (!res.ok || !res.body || !type.includes('text/event-stream')) {
          throw new Error(`stream unavailable (HTTP ${res.status})`);
        }
        this.reconnectDelay = this.serverRetry;
        this.stopPolling();
        // The stream being up says nothing about the snapshot: keep its error.
        this.patch({ connection: 'live' });
        await this.readStream(res.body, ctrl.signal, () => (lastByte = Date.now()));
        throw new Error('stream closed');
      } catch (err) {
        clearInterval(watchdog);
        if (this.stopped || (ctrl.signal.aborted && !stalled)) return;
        void err;
        this.patch({ connection: this.state.error ? 'offline' : 'polling' });
        this.schedulePoll(true);
        // A stalled stream is retried at once; a refused or closed one backs off.
        this.reconnectTimer = setTimeout(() => this.openStream(), stalled ? 0 : this.reconnectDelay);
        if (!stalled) this.reconnectDelay = Math.min(60000, this.reconnectDelay * 2);
      }
    })();
  }

  private async readStream(body: ReadableStream<Uint8Array>, signal: AbortSignal, onBytes: () => void): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let event = 'message';
    let data: string[] = [];
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) return;
      onBytes();
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.search(/\r\n|\r|\n/)) >= 0) {
        // A trailing CR may be the first half of CRLF split across reads: wait for more.
        if (buf[nl] === '\r' && nl === buf.length - 1) break;
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + (buf[nl] === '\r' && buf[nl + 1] === '\n' ? 2 : 1));
        if (line === '') {
          if (data.length) this.dispatch(event, data.join('\n'));
          event = 'message';
          data = [];
          continue;
        }
        if (line.startsWith(':')) continue;
        const i = line.indexOf(':');
        const field = i < 0 ? line : line.slice(0, i);
        let val = i < 0 ? '' : line.slice(i + 1);
        if (val.startsWith(' ')) val = val.slice(1);
        if (field === 'event') event = val;
        else if (field === 'data') data.push(val);
        else if (field === 'retry' && /^\d+$/.test(val)) this.serverRetry = this.reconnectDelay = +val;
      }
    }
  }

  private dispatch(event: string, data: string): void {
    if (event !== 'position') return;
    try {
      this.applyEvent(JSON.parse(data) as PositionEvent);
    } catch {
      /* malformed event: ignore, the next snapshot corrects state */
    }
  }

  // ---- polling -------------------------------------------------------------

  private schedulePoll(immediate = false): void {
    if (this.stopped || this.pollTimer) return;
    const tick = async () => {
      this.pollTimer = null;
      if (this.stopped || this.state.connection === 'live') return;
      await this.loadSnapshot();
      if ((this.state.connection as Connection) !== 'live') {
        this.patch({ connection: this.state.error ? 'offline' : 'polling' });
        this.pollTimer = setTimeout(tick, this.opts.pollIntervalMs);
      }
    };
    this.pollTimer = setTimeout(tick, immediate ? 0 : this.opts.pollIntervalMs);
  }

  private stopPolling(): void {
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.pollTimer = null;
  }

  // ---- notifications -------------------------------------------------------

  private patch(p: Partial<FeedState>): void {
    this.state = { ...this.state, ...p };
    if (p.lastDataAt) this.lastData = p.lastDataAt;
    this.bump(true);
  }

  private bump(now: boolean): void {
    const emit = () => {
      this.uiTimer = null;
      this.lastUiEmit = Date.now();
      const structural = this.structuralPending;
      this.structuralPending = false;
      this.state = {
        ...this.state,
        version: structural ? this.state.version + 1 : this.state.version,
        tick: this.state.tick + 1,
        count: this.shipments.size,
        lastDataAt: Math.max(this.state.lastDataAt ?? 0, this.lastData ?? 0) || null,
      };
      this.uiListeners.forEach((f) => f());
    };
    if (now) {
      if (this.uiTimer) clearTimeout(this.uiTimer);
      emit();
      return;
    }
    if (this.uiTimer) return;
    const wait = Math.max(0, 1000 - (Date.now() - this.lastUiEmit));
    this.uiTimer = setTimeout(emit, wait);
  }
}
