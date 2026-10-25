// Dense rate-card table shared by ops Rates and CarrierDetail (ux-benchmark 3.13).
import { compareRateBuy } from "@/lib/rateSort";
import React, { useState } from "react";
import { tEnum } from "@/lib/i18n";
import { Link } from "react-router-dom";
import { Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DataTable } from "@/components/app/DataTable";
import { num, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Dash, Lane, ModeIcon, MobileRow, RateState, SourcePill, Tip, eur, eurPrecise, isBlank, locCode, useIsNarrow } from "./common";

export function dateNum(iso, locale) {
  if (!iso) return null;
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00Z` : iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(locale === "fi" ? "fi-FI" : "en-GB", { day: "numeric", month: "numeric", year: "numeric" });
}

const CT_SHORT = { "20GP": "20'", "40GP": "40'", "40HC": "40HC", "45HC": "45HC", "20RF": "20RF", "40RF": "40RF" };

export function basisLabel(t, basis) {
  if (!basis) return null;
  const k = `oq.basis.${basis}`;
  const s = t(k);
  return s === k ? basis : s;
}

function tierRange(r, locale) {
  const tiers = (r.tiers || []).filter((x) => !isBlank(x.unit_cost_cents));
  if (!tiers.length) return null;
  const lo = Math.min(...tiers.map((x) => x.unit_cost_cents));
  const hi = Math.max(...tiers.map((x) => x.unit_cost_cents));
  return lo === hi ? eurPrecise(lo, locale) : `${eurPrecise(lo, locale)}–${eurPrecise(hi, locale)}`;
}

/** One-line buy price for narrow lists. */
export function buyShort(r, t, locale) {
  const cc = r.container_costs || {};
  if (!isBlank(cc["40GP"])) return `40' ${eur(cc["40GP"], locale)}`;
  const firstCc = Object.entries(cc).find(([, v]) => !isBlank(v));
  if (firstCc) return `${CT_SHORT[firstCc[0]] || firstCc[0]} ${eur(firstCc[1], locale)}`;
  if (!isBlank(r.unit_cost_cents)) return eur(r.unit_cost_cents, locale);
  return tierRange(r, locale) || "—";
}

/** Buy price cell: per-container sizes for FCL, else unit cost (or tier range) per basis; minimum underneath. */
export function BuyCell({ r }) {
  const { t, locale } = useT();
  const cc = r.container_costs && Object.entries(r.container_costs).filter(([, v]) => !isBlank(v));
  const minLine = !isBlank(r.min_cost_cents) && r.min_cost_cents > 0
    ? <span className="text-xs text-ink-tertiary">{t("oq.rates.min_short")} {eur(r.min_cost_cents, locale)}</span> : null;
  if (cc && cc.length) {
    const main = ["20GP", "40GP", "40HC"].filter((k) => !isBlank(r.container_costs[k]));
    const rest = cc.filter(([k]) => !main.includes(k));
    return (
      <span className="flex flex-col items-end whitespace-nowrap">
        {main.map((k) => (
          <span key={k} className="tabular"><span className="mr-1.5 text-xs text-ink-tertiary">{CT_SHORT[k]}</span>{eur(r.container_costs[k], locale)}</span>
        ))}
        {rest.length > 0 && (
          <Tip content={rest.map(([k, v]) => `${CT_SHORT[k] || k}: ${eur(v, locale)}`).join("\n")}>
            <span tabIndex={0} className="cursor-help text-xs text-ink-tertiary">{t("oq.rates.more_sizes", { n: rest.length })}</span>
          </Tip>
        )}
        {minLine}
      </span>
    );
  }
  const tiers = (r.tiers || []).filter((x) => !isBlank(x.unit_cost_cents));
  if (isBlank(r.unit_cost_cents) && tiers.length === 0) return <Dash />;
  const unit = basisLabel(t, r.basis);
  const tierTip = tiers.map((x) => `${isBlank(x.max_qty) ? t("oq.rates.tier_above") : `≤ ${num(x.max_qty, locale)}`}: ${eurPrecise(x.unit_cost_cents, locale)}`).join("\n");
  return (
    <span className="flex flex-col items-end whitespace-nowrap">
      <span className="tabular">
        {!isBlank(r.unit_cost_cents) ? eur(r.unit_cost_cents, locale) : tierRange(r, locale)}
        {unit && <span className="ml-1 text-xs text-ink-tertiary">{unit}</span>}
      </span>
      {tiers.length > 1 && (
        <Tip content={tierTip}>
          <span tabIndex={0} className="cursor-help text-xs text-ink-tertiary">{t("oq.rates.tiers_n", { n: tiers.length })}</span>
        </Tip>
      )}
      {minLine}
    </span>
  );
}

export function surchargeText(s, t, locale) {
  let v;
  if (!isBlank(s.pct)) v = `${num(s.pct, locale, 2)} %`;
  else if (s.by_size && Object.keys(s.by_size).length) v = Object.entries(s.by_size).map(([k, c]) => `${k}' ${eur(c, locale)}`).join(" / ");
  else v = eur(s.unit_cost_cents, locale);
  const b = basisLabel(t, s.basis);
  const min = !isBlank(s.min_cents) ? ` (${t("oq.rates.min_short")} ${eur(s.min_cents, locale)})` : "";
  const codeLabel = t(`review.charge.${s.code}`);
  return `${codeLabel === `review.charge.${s.code}` ? s.code || "—" : codeLabel} ${s.label ? `· ${s.label}` : ""}: ${v}${b ? ` ${b}` : ""}${min}`;
}

export function SurchargesCell({ list }) {
  const { t, locale } = useT();
  if (!list || list.length === 0) return <span className="text-ink-tertiary">{t("oq.rates.none")}</span>;
  return (
    <Tip content={list.map((s) => surchargeText(s, t, locale)).join("\n")}>
      <span tabIndex={0} className="cursor-help whitespace-nowrap">
        <span className="tabular">{list.length}</span>
        <span className="ml-1.5 text-xs text-ink-tertiary">{list.slice(0, 2).map((s) => { const k=`review.charge.${s.code}`; return t(k)===k?s.code:t(k); }).join(", ")}{list.length > 2 ? ", …" : ""}</span>
      </span>
    </Tip>
  );
}

export function Validity({ r }) {
  const { locale } = useT();
  return (
    <span className="flex flex-col gap-0.5 whitespace-nowrap">
      <span className="tabular text-sm">{dateNum(r.valid_from, locale) || "—"} – {dateNum(r.valid_to, locale) || "—"}</span>
      <RateState state={r.state} daysLeft={r.days_left} />
    </span>
  );
}

/** Sourcing Autopilot rates: RFQ link and approval status (pending rates do not price quotes). */
export function PartnerSource({ r }) {
  const { t } = useT();
  if (r.source !== "partner_network") return null;
  const d = r.source_detail || {};
  return (
    <span className="flex flex-col items-start gap-0.5 text-xs">
      {r.outreach_id && <Link to={`/ops/sourcing?rfq=${r.outreach_id}`} onClick={(e) => e.stopPropagation()} className="text-ink-accent hover:underline">{t("src.rfq_link", { ref: d.ref || "" })}</Link>}
      {r.status && r.status !== "accepted" && <span className={cn("font-medium", r.status === "pending" ? "text-warning-fg" : "text-ink-tertiary")}>{t(`src.status.${r.status}`)}</span>}
    </span>
  );
}

/** rows: RateCard[]; onEdit(row) when the user may edit; showCarrier for the cross-carrier view. */
export function RatesTable({ rows, onEdit, showCarrier = true, empty }) {
  const { t, locale } = useT();
  const narrow = useIsNarrow();
  const [page,setPage] = useState(0);
  const [unit,setUnit] = useState("");
  const filtered = unit ? rows.filter(r=>r.basis===unit) : rows;
  const current = Math.min(page,Math.max(0,Math.ceil(filtered.length/25)-1));
  if (rows.length === 0) return empty || null;
  if (narrow) {
    return (
      <div><ul className="divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
        {[...filtered].sort((a, b) => String(a.valid_to || "9").localeCompare(String(b.valid_to || "9"))).slice(current*25,(current+1)*25).map((r) => (
          <MobileRow key={r.id} onClick={onEdit ? () => onEdit(r) : undefined}
            primary={<span className="font-mono text-[13px]">{locCode(r.origin) || "—"} → {locCode(r.destination) || "—"}</span>}
            status={<RateState state={r.state} daysLeft={r.days_left} />}
            secondary={`${showCarrier ? `${r.carrier_name || "—"} · ` : ""}${r.service_name || r.service || ""} · ${dateNum(r.valid_from, locale) || "—"} – ${dateNum(r.valid_to, locale) || "—"}${r.source === "partner_network" ? ` · ${t("src.badge")}${r.status && r.status !== "accepted" ? ` (${t(`src.status.${r.status}`)})` : ""}` : ""}`}
            right={buyShort(r, t, locale)}
            rightSub={basisLabel(t, r.basis)} />
        ))}
      </ul>{filtered.length>25 && <nav className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs"><span>{t("review.rates.page",{page:current+1,pages:Math.ceil(filtered.length/25),n:filtered.length})}</span><Button size="sm" variant="outline" disabled={!current} onClick={()=>setPage(current-1)}>{t("review.previous")}</Button><Button size="sm" variant="outline" disabled={(current+1)*25>=filtered.length} onClick={()=>setPage(current+1)}>{t("review.next")}</Button></nav>}</div>
    );
  }
  const px = "px-2";
  const columns = [
    { key: "lane", header: t("oq.rates.col.lane"), sort: (r) => `${locCode(r.origin)}-${locCode(r.destination)}`,
      cell: (r) => <Lane origin={r.origin} destination={r.destination} via={r.via} />, className: `${px} max-w-[220px] pl-3`, headClassName: "pl-3" },
    { key: "carrier", header: showCarrier ? `${t("field.carrier")} · ${t("oq.rates.col.service")}` : t("oq.rates.col.service"),
      sort: (r) => `${showCarrier ? r.carrier_name : ""}-${r.mode}-${r.service}`, className: `${px} max-w-[240px]`, headClassName: px,
      cell: (r) => (
        <span className="flex min-w-0 flex-col">
          {showCarrier && (r.carrier_id
            ? <Link to={`/ops/carriers/${r.carrier_id}`} onClick={(e) => e.stopPropagation()} className="truncate hover:underline">{r.carrier_name || "—"}</Link>
            : <span className="truncate">{r.carrier_name || "—"}</span>)}
          <span className={cn("flex min-w-0 items-center gap-1.5 text-xs", showCarrier ? "text-ink-tertiary" : "text-sm")}>
            <ModeIcon mode={r.mode} className="size-3.5" />
            <span className="shrink-0">{t(`oq.service.${r.service}`)}</span>
            {r.service_name && <span className="truncate" title={r.service_name}>· {r.service_name}</span>}
          </span>
        </span>
      ) },
    { key: "validity", header: t("oq.rates.col.validity"), sort: (r) => r.valid_to, cell: (r) => <Validity r={r} />, className: px, headClassName: px },
    { key: "buy", header: t("review.rates.buy"), align: "right", compare: compareRateBuy, headTitle: t("review.rates.tier_sort"),
      cell: (r) => <BuyCell r={r} />, className: px, headClassName: px },
    { key: "surcharges", header: t("oq.rates.col.surcharges"), sort: (r) => (r.surcharges || []).length, cell: (r) => <SurchargesCell list={r.surcharges} />, className: px, headClassName: px },
    { key: "transit_days", header: t("oq.rates.col.transit"), align: "right", className: px, headClassName: px,
      cell: (r) => (isBlank(r.transit_days) ? <Dash /> : <span className="whitespace-nowrap">{t("oq.days", { n: num(r.transit_days, locale) })}</span>) },
    { key: "source", header: t("oq.rates.col.source"), sort: (r) => r.updated_at, className: px, headClassName: px,
      cell: (r) => (
        <span className="flex flex-col items-start gap-0.5 whitespace-nowrap">
          <SourcePill source={r.source} />
          <PartnerSource r={r} />
          <span className="text-xs text-ink-tertiary" title={`${r.updated_at || ""}${r.updated_by?.name ? ` · ${r.updated_by.name}` : ""}`}>{r.updated_at ? relTime(r.updated_at, locale) : "—"}</span>
        </span>
      ) },
    onEdit && { key: "edit", header: "", sortable: false, className: "w-10 pr-2",
      cell: (r) => (
        <Button size="icon-xs" variant="ghost" aria-label={t("oq.rates.edit")} onClick={(e) => { e.stopPropagation(); onEdit(r); }}>
          <Pencil />
        </Button>
      ) },
  ].filter(Boolean);
  return (
    <div className="rounded-xl border border-[color:var(--border-subtle)]">
      <label className="flex items-center gap-2 px-3 py-2 text-xs">{t("review.rates.unit")}<select className="surface-input rounded-md px-2 py-1" value={unit} onChange={e=>{setUnit(e.target.value);setPage(0);}}><option value="">{t("review.rates.all_units")}</option>{[...new Set(rows.map(r=>r.basis))].sort().map(b=><option key={b} value={b}>{basisLabel(t,b)}</option>)}</select></label>
      <DataTable columns={columns} rows={filtered} pageSize={25} dense onRowClick={onEdit} defaultSort={{ key: "validity", dir: "asc" }}
        className="min-w-[1080px]" rowClassName={(r) => cn(r.state === "expired" && "text-ink-secondary")} />
    </div>
  );
}
