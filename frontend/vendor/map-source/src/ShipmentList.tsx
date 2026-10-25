import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import type { PreparedShipment } from './prepare';
import type { Locale } from './types';
import { fmtDate, type Strings } from './i18n';
import { ModeGlyph, StatusBadge } from './ui';

const ROW = 60;
const OVERSCAN = 6;

export interface ShipmentListProps {
  items: PreparedShipment[];
  S: Strings;
  locale: Locale;
  selectedId: string | null;
  showCustomer: boolean;
  onSelect: (id: string) => void;
  onHover?: (id: string | null) => void;
  label: string;
}

/** Windowed list: stays fast with thousands of rows; arrow keys move the selection. */
export function ShipmentList({ items, S, locale, selectedId, showCustomer, onSelect, onHover, label }: ShipmentListProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(600);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setHeight(el.clientHeight));
    ro.observe(el);
    setHeight(el.clientHeight);
    return () => ro.disconnect();
  }, []);

  // Keep the selected row in view when selection comes from the map.
  useEffect(() => {
    const el = scroller.current;
    if (!el || !selectedId) return;
    const i = items.findIndex((p) => p.s.id === selectedId);
    if (i < 0) return;
    const top = i * ROW;
    if (top < el.scrollTop) el.scrollTop = top;
    else if (top + ROW > el.scrollTop + el.clientHeight) el.scrollTop = top + ROW - el.clientHeight;
  }, [selectedId, items]);

  const onKey = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      e.preventDefault();
      const i = items.findIndex((p) => p.s.id === selectedId);
      const next = Math.max(0, Math.min(items.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)));
      const id = items[next]?.s.id;
      if (id) {
        onSelect(id);
        requestAnimationFrame(() => {
          (scroller.current?.querySelector(`[data-id="${id}"]`) as HTMLElement | null)?.focus({ preventScroll: true });
        });
      }
    },
    [items, selectedId, onSelect],
  );

  const first = Math.max(0, Math.floor(scrollTop / ROW) - OVERSCAN);
  const last = Math.min(items.length, Math.ceil((scrollTop + height) / ROW) + OVERSCAN);
  const rows = items.slice(first, last);

  return (
    <div
      className="huo-list__scroll"
      ref={scroller}
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
      onKeyDown={onKey}
      role="list"
      aria-label={label}
      onMouseLeave={() => onHover?.(null)}
    >
      <div style={{ height: items.length * ROW, position: 'relative' }}>
        {rows.map((p, k) => {
          const s = p.s;
          const i = first + k;
          return (
            <div role="listitem" key={s.id} style={{ position: 'absolute', top: i * ROW, left: 0, right: 0, height: ROW }}>
              <button
                type="button"
                className="huo-row"
                data-id={s.id}
                aria-current={s.id === selectedId ? 'true' : undefined}
                data-attention={p.attention || undefined}
                onClick={() => onSelect(s.id)}
                onMouseEnter={() => onHover?.(s.id)}
                onFocus={() => onHover?.(s.id)}
              >
                <ModeGlyph mode={s.mode} />
                <span className="huo-row__main">
                  <span className="huo-row__top">
                    <span className="huo-mono huo-row__ref">{s.reference}</span>
                    {showCustomer && <span className="huo-muted huo-truncate">{s.customer_name}</span>}
                  </span>
                  <span className="huo-row__route huo-truncate">
                    {s.origin.name} → {s.destination.name}
                  </span>
                </span>
                <span className="huo-row__side">
                  <StatusBadge status={s.status} S={S} />
                  <span className="huo-muted huo-small huo-num">
                    {s.status === 'delivered' ? '' : `${S.eta} `}
                    {fmtDate(s.eta, locale, false)}
                  </span>
                </span>
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
