// Claims table (portal + ops). Stacked rows below 768 px.
import React from "react";
import { DataTable } from "@/components/app/DataTable";
import { StatusBadge } from "@/components/app/StatusBadge";
import { money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { TimeBarText } from "./CapCalculation";
import { isDecided, typeLabel } from "./text";

const eur = (c, l) => money(c, l, { decimals: 2 });

function Late() {
  const { t } = useT();
  return <StatusBadge size="sm" state="pending" label={t("claims.late_notice")} icon={false} className="h-[18px]" />;
}

export default function ClaimsList({ rows, mode = "customer", selected, onOpen, narrow }) {
  const { t, locale } = useT();
  const ops = mode === "ops";

  if (narrow) {
    return (
      <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)] rounded-xl border border-[color:var(--border-subtle)]">
        {rows.map((c) => (
          <li key={c.id}>
            <button type="button" onClick={() => onOpen(c.id)} className={cn("flex w-full flex-col gap-1 px-3 py-2.5 text-left hover:bg-hover", selected === c.id && "bg-subtle")}>
              <span className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium tabular">{c.number}</span>
                <StatusBadge size="sm" group="claim" value={c.status} />
              </span>
              <span className="text-sm">{ops ? `${c.company_name} · ` : ""}{c.shipment_reference} · {typeLabel(t, c.type)}</span>
              <span className="flex items-end justify-between gap-2 text-xs">
                <span className="text-ink-secondary">
                  <span className="font-medium text-foreground tabular">{eur(c.amount_claimed_cents, locale)}</span>
                  <span className="text-ink-tertiary"> / {t("claims.col.cap").toLowerCase()} {eur(c.cap_eur_cents, locale)}</span>
                </span>
                <TimeBarText iso={c.time_bar_date} decided={isDecided(c.status)} className="items-end text-right" />
              </span>
              {c.notice_late && ops && <span><Late /></span>}
            </button>
          </li>
        ))}
      </ul>
    );
  }

  const columns = [
    { key: "number", header: t("claims.col.number"), className: "whitespace-nowrap",
      cell: (c) => (
        <span className="flex flex-col">
          <span className="text-sm font-medium tabular">{c.number}</span>
          <span className="text-xs text-ink-tertiary">{ops ? c.company_name : typeLabel(t, c.type)}</span>
        </span>
      ) },
    { key: "shipment_reference", header: t("claims.col.shipment"), className: "whitespace-nowrap",
      cell: (c) => (
        <span className="flex flex-col">
          <span className="text-sm">{c.shipment_reference}</span>
          {ops && <span className="text-xs text-ink-tertiary">{typeLabel(t, c.type)}</span>}
        </span>
      ) },
    { key: "amount_claimed_cents", header: t("claims.col.claimed"), align: "right", className: "whitespace-nowrap",
      cell: (c) => <span className="font-medium">{eur(c.amount_claimed_cents, locale)}</span> },
    { key: "cap_eur_cents", header: t("claims.col.cap"), align: "right", className: "whitespace-nowrap",
      cell: (c) => (
        <span className={cn(c.cap_eur_cents != null && c.amount_claimed_cents > c.cap_eur_cents ? "text-warning-fg" : "text-ink-secondary")}>
          {eur(c.cap_eur_cents, locale)}
        </span>
      ) },
    { key: "time_bar_date", header: t("claims.col.time_bar"), className: "whitespace-nowrap",
      cell: (c) => <TimeBarText iso={c.time_bar_date} decided={isDecided(c.status)} /> },
    { key: "status", header: t("claims.col.status"), className: "whitespace-nowrap",
      cell: (c) => (
        <span className="flex flex-col items-start gap-1">
          <StatusBadge size="sm" group="claim" value={c.status} />
          {ops && c.notice_late && <Late />}
        </span>
      ) },
  ];
  return (
    <div className="overflow-x-auto rounded-xl border border-[color:var(--border-subtle)]">
      <DataTable dense columns={columns} rows={rows} onRowClick={(c) => onOpen(c.id)} rowClassName={(c) => cn(selected === c.id && "bg-subtle")} />
    </div>
  );
}
