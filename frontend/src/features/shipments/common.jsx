// Shared bits for V1 shipment screens (portal, ops, partner): mode glyphs, route text, ETA cell, sources, stale note.
import React, { Component } from "react";
import { AlertTriangle, Layers, Plane, RefreshCw, Ship, TrainFront, Truck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Freshness } from "@/components/app/states";
import { tEnum, useT } from "@/lib/i18n";
import { date, dateShort, dateTime, relTime } from "@/lib/format";
import { cn } from "@/lib/utils";

export const STATUS_ORDER = ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "exception", "delivered", "cancelled"];
export const ACTIVE_STATUSES = ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "exception"];
export const MODES = ["sea", "road", "air", "rail"];
export const HOLD_TYPES = ["customs", "inspection", "documents", "sanctions", "lien", "payment", "other"];
export const DELIVERY_BLOCKING = new Set(["customs", "inspection", "documents", "sanctions", "lien", "payment"]);
// Mirrors services/shipments.py TRANSITIONS (the API enforces it; the UI only offers what is allowed).
export const TRANSITIONS = {
  booked: ["picked_up", "in_transit", "at_hub", "customs_hold", "exception", "cancelled"],
  picked_up: ["in_transit", "at_hub", "customs_hold", "out_for_delivery", "delivered", "exception", "cancelled"],
  in_transit: ["at_hub", "customs_hold", "out_for_delivery", "delivered", "exception"],
  at_hub: ["in_transit", "customs_hold", "out_for_delivery", "delivered", "exception"],
  customs_hold: ["at_hub", "in_transit", "out_for_delivery", "exception"],
  out_for_delivery: ["delivered", "at_hub", "exception"],
  exception: ["booked", "picked_up", "in_transit", "at_hub", "customs_hold", "out_for_delivery", "delivered", "cancelled"],
  delivered: [],
  cancelled: [],
};
export const RECORDABLE_MILESTONES = ["picked_up", "received", "export_cleared", "departed", "transshipment", "arrived", "customs_released",
  "out_for_delivery", "delivered", "note"];

const MODE_ICON = { sea: Ship, road: Truck, air: Plane, rail: TrainFront, multimodal: Layers };
// Same series tokens the map uses for leg lines (s2: sea chart-1, road chart-7, rail chart-6, air chart-3).
export const MODE_COLOR = { sea: "var(--chart-1)", road: "var(--chart-7)", rail: "var(--chart-6)", air: "var(--chart-3)" };

export function ModeIcon({ mode, className, colored }) {
  const Icon = MODE_ICON[mode] || Layers;
  return <Icon className={cn("size-4 shrink-0", !colored && "text-ink-secondary", className)} style={colored ? { color: MODE_COLOR[mode] } : undefined} aria-hidden />;
}

export function ModeLabel({ mode, className }) {
  const { t } = useT();
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", className)}>
      <ModeIcon mode={mode} />
      {tEnum(t, "mode", mode)}
    </span>
  );
}

const SUFFIXES = [" International Inland Port", " Inland Port", " International Airport", " Airport", " Harbour", " Harbor", " Port", " Terminal"];
/** Short place name for dense rows: "Helsinki Vuosaari Harbour" -> "Helsinki Vuosaari". */
export function shortPlace(p) {
  if (!p) return "—";
  let n = p.name || p.city || p.unlocode || "—";
  for (const s of SUFFIXES) if (n.endsWith(s) && n.length > s.length + 2) n = n.slice(0, -s.length);
  return n;
}

export function RouteText({ origin, destination, className, short = true }) {
  const a = short ? shortPlace(origin) : origin?.name || "—";
  const b = short ? shortPlace(destination) : destination?.name || "—";
  return (
    <span className={cn("min-w-0 truncate", className)} title={`${origin?.name || "—"} → ${destination?.name || "—"}`}>
      {a} <span className="text-ink-tertiary">→</span> {b}
    </span>
  );
}

export function timeHM(d, locale) {
  const x = d instanceof Date ? d : new Date(d);
  if (Number.isNaN(x.getTime())) return "—";
  return x.toLocaleTimeString(locale === "fi" ? "fi-FI" : "en-GB", { hour: "2-digit", minute: "2-digit" });
}

/** ETA cell: delivered date, or ETA + "late by N d" / "past ETA". Right-aligned by the caller. */
export function EtaValue({ s, compact }) {
  const { t, locale } = useT();
  if (s.status === "delivered") {
    return (
      <span className="inline-flex flex-col items-end leading-tight">
        <span className="tabular">{dateShort(s.delivered_at || s.eta, locale)}</span>
        {!compact && <span className="text-xs text-ink-tertiary">{t("shipments.detail.delivered")}</span>}
      </span>
    );
  }
  if (s.status === "cancelled") return <span className="text-ink-tertiary">—</span>;
  const overdue = s.eta && new Date(s.eta).getTime() < Date.now();
  const late = s.delay_days > 0;
  return (
    <span className="inline-flex flex-col items-end leading-tight">
      <span className="tabular">{s.eta ? dateShort(s.eta, locale) : "—"}</span>
      {overdue ? (
        <span className="whitespace-nowrap text-xs text-danger-fg">{t("shipments.past_eta")}</span>
      ) : late ? (
        <span className="whitespace-nowrap text-xs text-warning-fg">{t("shipments.late_by", { n: s.delay_days })}</span>
      ) : !compact ? (
        <span className="text-xs text-ink-tertiary">{t("shipments.eta_est")}</span>
      ) : null}
    </span>
  );
}

export function sourceLabel(t, source) {
  if (!source) return "—";
  const k = `shipments.source.${source}`;
  const s = t(k);
  return s === k ? source : s;
}

export function msLabel(t, m) {
  if (!m) return "—";
  const k = `shipments.ms.${m.code}`;
  const s = t(k);
  return s === k ? m.label || m.code : s;
}

export function holdLabel(t, type) {
  const k = `shipments.hold.${type}`;
  const s = t(k);
  return s === k ? type : s;
}

export function reasonLabel(t, r) {
  const k = `shipments.reason.${r}`;
  const s = t(k);
  return s === k ? r : s;
}

/** Needs-action reasons as small text chips (state colour only on exception/hold). */
export function Reasons({ reasons, className, max = 3 }) {
  const { t } = useT();
  if (!reasons || reasons.length === 0) return null;
  const shown = reasons.slice(0, max);
  return (
    <span className={cn("inline-flex flex-wrap gap-1", className)}>
      {shown.map((r) => (
        <span key={r} className={cn("inline-flex h-5 items-center gap-1 rounded-full px-1.5 text-[11px] font-medium whitespace-nowrap",
          r === "exception" || r === "overdue" ? "status-failed" : r === "hold" || r === "late" || r === "milestone_overdue" ? "status-pending" : "bg-secondary text-ink-secondary")}>
          {(r === "exception" || r === "hold") && <AlertTriangle className="size-3" aria-hidden />}
          {reasonLabel(t, r)}
        </span>
      ))}
      {reasons.length > max && <span className="text-[11px] text-ink-tertiary">+{reasons.length - max}</span>}
    </span>
  );
}

/** Freshness, or "Tiedot ajalta hh:mm" + Retry when the last refresh failed (data stays on screen). */
export function DataAge({ q, className }) {
  const { t, locale } = useT();
  if (!q?.updatedAt) return null;
  if (q.error && q.data) {
    return (
      <span className={cn("inline-flex items-center gap-1.5 text-xs text-warning-fg", className)} role="status">
        <AlertTriangle className="size-3.5" aria-hidden />
        {t("shipments.stale", { time: timeHM(q.updatedAt, locale) })}
        <Button size="xs" variant="outline" onClick={q.reload}>{t("action.retry")}</Button>
      </span>
    );
  }
  if (q.refreshing) {
    return (
      <span className={cn("inline-flex items-center gap-1.5 text-xs text-ink-tertiary", className)} role="status">
        <RefreshCw className="size-3.5 animate-spin" aria-hidden /> {t("shipments.updating")}
      </span>
    );
  }
  return <span className={className}><Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} /></span>;
}

export function DemoTag({ show }) {
  const { t } = useT();
  if (!show) return null;
  return <span title={t("shipments.demo_title")} className="inline-flex h-5 items-center rounded-full bg-secondary px-1.5 text-[11px] text-ink-tertiary">{t("shipments.demo")}</span>;
}

export function LastUpdate({ e }) {
  const { t, locale } = useT();
  if (!e) return <span className="text-ink-tertiary">—</span>;
  return (
    <span className="inline-flex flex-col leading-tight min-w-0" title={`${sourceLabel(t, e.source)} · ${dateTime(e.at, locale)}`}>
      <span className="truncate">{msLabel(t, e)}</span>
      <span className="text-xs text-ink-tertiary">{relTime(e.at, locale)}</span>
    </span>
  );
}

/** Embedded panel from another slice: a failure there must not take the shipment page down. */
export class PanelBoundary extends Component {
  state = { error: null };
  static getDerivedStateFromError(error) { return { error }; }
  componentDidCatch(error) { console.error("Embedded panel failed", error); } // eslint-disable-line no-console
  render() {
    if (this.state.error) {
      return <div role="alert" className="rounded-lg border border-dashed px-3 py-3 text-sm text-ink-tertiary">{this.props.message}</div>;
    }
    return this.props.children;
  }
}

export function fmtKm(v, locale) {
  if (v === null || v === undefined) return "—";
  return `${new Intl.NumberFormat(locale === "fi" ? "fi-FI" : "en-GB", { maximumFractionDigits: 0 }).format(v)} km`;
}

/** "3 d" / "5 h" duration between two ISO times, localised unit. */
export function durText(ms, locale) {
  const h = Math.abs(ms) / 3600000;
  if (h >= 36) return `${Math.round(h / 24)} ${locale === "fi" ? "pv" : "d"}`;
  if (h >= 1) return `${Math.round(h)} h`;
  return `${Math.max(1, Math.round(h * 60))} min`;
}

export { date, dateShort, dateTime, relTime };
