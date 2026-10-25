import type { ComponentType, ReactNode, SVGProps } from 'react';
import {
  CalendarClock,
  CircleCheck,
  CircleDashed,
  CircleX,
  Layers,
  Plane,
  Ship,
  TrainFront,
  TriangleAlert,
  Truck,
} from 'lucide-react';
import type { Mode, Status } from './types';
import type { Strings } from './i18n';

type Icon = ComponentType<SVGProps<SVGSVGElement> & { size?: number; strokeWidth?: number }>;

/** Contract status → Factory workflow state (hue + glyph). No new status meanings. */
export const STATUS_TONE: Record<Status, { tone: string; Icon: Icon }> = {
  booked: { tone: 'scheduled', Icon: CalendarClock },
  picked_up: { tone: 'in-progress', Icon: CircleDashed },
  in_transit: { tone: 'in-progress', Icon: CircleDashed },
  at_hub: { tone: 'in-progress', Icon: CircleDashed },
  customs_hold: { tone: 'pending', Icon: TriangleAlert },
  out_for_delivery: { tone: 'in-progress', Icon: CircleDashed },
  delivered: { tone: 'success', Icon: CircleCheck },
  exception: { tone: 'failed', Icon: CircleX },
};

export const MODE_ICON: Record<Mode, Icon> = {
  sea: Ship,
  road: Truck,
  air: Plane,
  rail: TrainFront,
  multimodal: Layers,
};

export function StatusBadge({ status, S, size = 'sm' }: { status: Status; S: Strings; size?: 'sm' | 'md' }) {
  const { tone, Icon } = STATUS_TONE[status] ?? STATUS_TONE.in_transit;
  return (
    <span className="huo-badge" data-tone={tone} data-size={size}>
      <Icon aria-hidden size={size === 'sm' ? 12 : 14} strokeWidth={2} />
      {S.status[status] ?? status}
    </span>
  );
}

export function ModeGlyph({ mode, size = 14 }: { mode: Mode; size?: number }) {
  const Icon = MODE_ICON[mode] ?? Layers;
  return (
    <span className="huo-mode-glyph" data-mode={mode} aria-hidden>
      <Icon size={size} strokeWidth={1.75} />
    </span>
  );
}

export function IconButton({
  label,
  onClick,
  children,
  pressed,
  className,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
  pressed?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      className={`huo-icon-btn ${className ?? ''}`}
      aria-label={label}
      title={label}
      aria-pressed={pressed}
      onClick={onClick}
    >
      {children}
    </button>
  );
}
