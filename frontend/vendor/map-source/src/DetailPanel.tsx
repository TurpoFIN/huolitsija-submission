import { useEffect, useRef, type MouseEvent } from 'react';
import { ArrowUpRight, CircleX, TriangleAlert, X } from 'lucide-react';
import type { PreparedShipment } from './prepare';
import type { Locale, Shipment } from './types';
import { fmtDate, fmtInt, fmtRelative, type Strings } from './i18n';
import { IconButton, ModeGlyph, StatusBadge } from './ui';

export interface DetailPanelProps {
  p: PreparedShipment;
  S: Strings;
  locale: Locale;
  now: number;
  staleAfterMs: number;
  showCustomer: boolean;
  href: string;
  onNavigate?: (href: string, shipment: Shipment, event: MouseEvent<HTMLAnchorElement>) => void;
  onClose: () => void;
}

export function DetailPanel({ p, S, locale, now, staleAfterMs, showCustomer, href, onNavigate, onClose }: DetailPanelProps) {
  const s = p.s;
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [s.id]);

  const pct = Math.round(Math.max(0, Math.min(1, s.progress)) * 100);
  const kmLeft = Math.max(0, Math.round(p.totalKm * (1 - s.progress)));
  const posAge = now - Date.parse(s.position.updated_at);
  const stale = Number.isFinite(posAge) && posAge > staleAfterMs;
  const estimated = s.position.source === 'eta_interpolation';
  const exception = s.status === 'exception' || s.has_exception;
  const hold = s.status === 'customs_hold';

  return (
    <aside className="huo-panel" aria-labelledby="huo-panel-title" onKeyDown={(e) => e.key === 'Escape' && onClose()}>
      <div className="huo-panel__head">
        <div className="huo-panel__titles">
          <h2 id="huo-panel-title" ref={headingRef} tabIndex={-1} className="huo-panel__ref">
            {s.reference}
          </h2>
          {showCustomer && <div className="huo-muted huo-truncate">{s.customer_name}</div>}
        </div>
        <IconButton label={S.close} onClick={onClose}>
          <X size={14} aria-hidden />
        </IconButton>
      </div>

      <div className="huo-panel__status">
        <StatusBadge status={s.status} S={S} size="md" />
        <span className="huo-chip-quiet">
          <ModeGlyph mode={s.mode} size={13} />
          {S.mode[s.mode]}
        </span>
      </div>

      {(exception || hold) && (
        <div className="huo-note" data-tone={exception ? 'danger' : 'warning'} role="status">
          {exception ? <CircleX size={14} aria-hidden /> : <TriangleAlert size={14} aria-hidden />}
          <span>{exception ? S.exceptionNote : S.holdNote}</span>
        </div>
      )}

      <section className="huo-panel__section" aria-label={S.route}>
        <div className="huo-route">
          <div className="huo-route__end">
            <span className="huo-route__name">{s.origin.name}</span>
            {s.origin.unlocode && <span className="huo-mono huo-muted">{s.origin.unlocode}</span>}
          </div>
          <span className="huo-route__arrow" aria-hidden>→</span>
          <div className="huo-route__end huo-route__end--to">
            <span className="huo-route__name">{s.destination.name}</span>
            {s.destination.unlocode && <span className="huo-mono huo-muted">{s.destination.unlocode}</span>}
          </div>
        </div>
        <div
          className="huo-progress"
          role="progressbar"
          aria-label={S.progress}
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <span style={{ width: `${pct}%` }} />
        </div>
        <div className="huo-progress__meta huo-muted">
          <span className="huo-num">{pct}%</span>
          {p.totalKm > 0 && s.status !== 'delivered' && <span className="huo-num">{fmtInt(kmLeft, locale)} km</span>}
        </div>
      </section>

      <dl className="huo-facts">
        <dt>{S.eta}</dt>
        <dd>
          {s.eta ? (
            <>
              <span className="huo-num">{fmtDate(s.eta, locale)}</span>
              <span className="huo-muted"> · {fmtRelative(s.eta, now, locale)}</span>
            </>
          ) : (
            <span className="huo-muted">{S.unknown}</span>
          )}
        </dd>
        <dt>{S.position}</dt>
        <dd>
          <span>{S.source[s.position.source] ?? s.position.source}</span>
          {/* A report can't be from the future; clock skew between server and browser reads as "just now". */}
          <span className="huo-muted"> · {fmtRelative(Math.min(Date.parse(s.position.updated_at), now), now, locale)}</span>
          {(stale || estimated) && (
            <span className="huo-tag" data-tone={stale ? 'warning' : 'neutral'}>
              {stale ? S.stale : S.estimated}
            </span>
          )}
          <div className="huo-mono huo-muted huo-small">
            {s.position.lat.toFixed(3)}, {s.position.lon.toFixed(3)}
          </div>
        </dd>
        {s.eta_prediction?.state === 'predicted' && <>
          <dt>{locale === 'fi' ? 'Etapin ennuste' : 'Leg forecast'}</dt>
          <dd>
            <div className="huo-num">{fmtDate(s.eta_prediction.eta!, locale)}</div>
            <div className="huo-muted huo-small">{locale === 'fi' ? 'Vaihteluväli' : 'Range'}: {fmtDate(s.eta_prediction.earliest!, locale)} – {fmtDate(s.eta_prediction.latest!, locale)}</div>
            <div className="huo-muted huo-small">{locale === 'fi' ? 'Rajallinen varmuus · vakionopeusmalli' : 'Limited confidence · constant-speed model'}</div>
            <details className="huo-small"><summary>{locale === 'fi' ? 'Laskennan lähtötiedot' : 'Forecast inputs'}</summary>
              <div>{s.eta_prediction.inputs?.distance_km} km · {s.eta_prediction.inputs?.speed_kmh} km/h · {s.eta_prediction.inputs?.routing}</div>
            </details>
          </dd>
        </>}
        <dt>{S.cargo}</dt>
        <dd className="huo-num">
          {s.weight_kg != null ? `${fmtInt(Math.round(s.weight_kg), locale)} kg` : <span className="huo-muted">{S.unknown}</span>}
          {s.teu != null && s.teu > 0 && ` · ${s.teu} ${S.teu}`}
        </dd>
      </dl>

      <section className="huo-panel__section" aria-label={S.legs}>
        <h3 className="huo-label">{S.legs}</h3>
        <ol className="huo-legs">
          {s.legs.map((l, i) => {
            const state = i < p.currentLeg || s.status === 'delivered' ? 'done' : i === p.currentLeg ? 'current' : 'next';
            return (
              <li key={l.seq} className="huo-leg" data-state={state}>
                <ModeGlyph mode={l.mode} size={13} />
                <div className="huo-leg__body">
                  <div className="huo-leg__route huo-truncate">
                    {l.from.name} → {l.to.name}
                  </div>
                  <div className="huo-muted huo-small huo-truncate">
                    {l.carrier}
                    {' · '}
                    {l.atd ? `${S.atd} ${fmtDate(l.atd, locale)}` : `${S.etd} ${fmtDate(l.etd, locale)}`}
                    {' · '}
                    {l.ata ? `${S.ata} ${fmtDate(l.ata, locale)}` : `${S.eta} ${fmtDate(l.eta, locale)}`}
                  </div>
                </div>
                {state === 'current' && s.status !== 'booked' && <span className="huo-tag">{S.current}</span>}
              </li>
            );
          })}
        </ol>
      </section>

      <div className="huo-panel__foot">
        <a
          className="huo-btn huo-btn--primary"
          href={href}
          onClick={(e) => {
            if (!onNavigate || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
            e.preventDefault();
            onNavigate(href, s, e);
          }}
        >
          {S.openShipment}
          <ArrowUpRight size={14} aria-hidden />
        </a>
      </div>
    </aside>
  );
}
