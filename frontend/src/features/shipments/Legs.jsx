// Legs: one row per leg (mode, from -> to, carrier + vehicle, departure/arrival actual vs estimate, distance).
// Ops can edit a leg and pick a carrier via V2's CarrierSelectionPanel.
import React, { Suspense, lazy, useState } from "react";
import { Pencil, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { apiPatch } from "@/lib/api";
import { tEnum, useT } from "@/lib/i18n";
import { displayText } from "@/lib/localizedData";
import { cn } from "@/lib/utils";
import { PanelBoundary, ModeIcon, dateTime, fmtKm, shortPlace } from "@/features/shipments/common";

const CarrierSelectionPanel = lazy(() => import("@/features/quotes/CarrierSelectionPanel"));

function vehicle(t, lg) {
  if (lg.vessel?.name) return t("shipments.leg.vessel", { name: lg.vessel.name }) + (lg.voyage ? ` · ${lg.voyage}` : "");
  if (lg.flight) return t("shipments.leg.flight", { no: lg.flight });
  if (lg.train?.number) return t("shipments.leg.train", { no: lg.train.number });
  if (lg.truck_plate) return t("shipments.leg.truck", { plate: lg.truck_plate });
  return null;
}

function legStatus(t, lg) {
  const v = lg.ata ? "completed" : lg.atd ? "in_progress" : lg.status === "completed" ? "completed" : "planned";
  return { v, label: t(`shipments.leg.status.${v}`) };
}

function When({ actual, planned }) {
  const { t, locale } = useT();
  if (actual) return <span className="tabular">{dateTime(actual, locale)}</span>;
  if (!planned) return <span className="text-ink-tertiary">—</span>;
  return (
    <span className="tabular text-ink-tertiary">
      {dateTime(planned, locale)} <span className="italic">({t("shipments.ms.estimate")})</span>
    </span>
  );
}

export function LegsTable({ shipment, ops, onChanged }) {
  const { t, locale } = useT();
  const [edit, setEdit] = useState(null);
  const [assign, setAssign] = useState(null);
  const legs = shipment.legs || [];
  const locked = ["delivered", "cancelled"].includes(shipment.status);
  return (
    <>
      <div className="mb-1 hidden md:grid md:grid-cols-[20px_minmax(0,1.5fr)_minmax(0,1.1fr)_minmax(0,0.9fr)_minmax(0,0.9fr)_84px_150px] gap-x-3 text-[11px] text-ink-tertiary">
        <span />
        <span>{t("shipments.leg.route")}</span>
        <span>{t("shipments.leg.carrier")}</span>
        <span>{t("shipments.leg.departure")}</span>
        <span>{t("shipments.leg.arrival")}</span>
        <span className="text-right">{t("shipments.leg.distance")}</span>
        <span />
      </div>
      <ol className="divide-y divide-[color:var(--border-subtle)] border-y border-[color:var(--border-subtle)]">
        {legs.map((lg) => {
          const st = legStatus(t, lg);
          const veh = vehicle(t, lg);
          return (
            <li key={lg.seq} className={cn("grid grid-cols-[20px_minmax(0,1fr)] gap-x-3 py-2.5 md:grid-cols-[20px_minmax(0,1.5fr)_minmax(0,1.1fr)_minmax(0,0.9fr)_minmax(0,0.9fr)_84px_150px] md:items-center",
              st.v === "in_progress" && "bg-[var(--bg-subtle)]")}>
              <span className="pt-0.5 md:pt-0" title={tEnum(t, "mode", lg.mode)}><ModeIcon mode={lg.mode} colored /></span>
              <div className="min-w-0">
                <p className="line-clamp-2 text-sm" title={`${lg.from?.name || "—"} → ${lg.to?.name || "—"}`}>
                  {shortPlace(lg.from)} <span className="text-ink-tertiary">→</span> {shortPlace(lg.to)}
                </p>
                <p className="truncate text-xs text-ink-tertiary">
                  {[`${lg.seq}. ${t(`shipments.leg.role.${lg.role || "main"}`)}`, displayText(lg.service, locale),
                    ops && lg.path_source ? t(`shipments.leg.path.${lg.path_source}`) : null].filter(Boolean).join(" · ")}
                </p>
                {ops && lg.path_note && <p className="truncate text-xs text-ink-tertiary" title={lg.path_note}>{lg.path_note}</p>}
              </div>
              <div className="col-start-2 mt-1 min-w-0 md:col-start-auto md:mt-0">
                <p className={cn("truncate text-sm", !lg.carrier && "text-ink-tertiary")}>{lg.carrier || t("shipments.no_carrier")}</p>
                <p className="truncate text-xs text-ink-tertiary">{veh || " "}</p>
              </div>
              <div className="col-start-2 text-sm md:col-start-auto">
                <span className="text-xs text-ink-tertiary md:hidden">{t("shipments.leg.departure")} </span>
                <When actual={lg.atd} planned={lg.etd} />
              </div>
              <div className="col-start-2 text-sm md:col-start-auto">
                <span className="text-xs text-ink-tertiary md:hidden">{t("shipments.leg.arrival")} </span>
                <When actual={lg.ata} planned={lg.eta} />
              </div>
              <div className="col-start-2 text-sm tabular text-ink-secondary md:col-start-auto md:text-right">
                {fmtKm(lg.distance_km, locale)}
              </div>
              <div className="col-start-2 mt-1 flex items-center gap-1 md:col-start-auto md:mt-0 md:justify-end">
                <span className={cn("text-xs", st.v === "in_progress" ? "text-foreground" : "text-ink-tertiary")}>{st.label}</span>
                {ops && !locked && !lg.ata && (
                  <>
                    <Button size="icon-xs" variant="ghost" aria-label={lg.carrier ? t("shipments.leg.change_carrier") : t("shipments.leg.assign")}
                      title={lg.carrier ? t("shipments.leg.change_carrier") : t("shipments.leg.assign")} onClick={() => setAssign(lg)}>
                      <UserPlus />
                    </Button>
                    <Button size="icon-xs" variant="ghost" aria-label={t("shipments.leg.edit")} title={t("shipments.leg.edit")} onClick={() => setEdit(lg)}>
                      <Pencil />
                    </Button>
                  </>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      {edit && <EditLegDialog shipment={shipment} leg={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); onChanged?.(); }} />}
      <Sheet open={!!assign} onOpenChange={(o) => !o && setAssign(null)}>
        <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
          <SheetHeader>
            <SheetTitle>{assign ? t("shipments.leg.carrier_sheet", { seq: assign.seq }) : ""}</SheetTitle>
            <SheetDescription>{assign ? `${assign.from?.name || "—"} → ${assign.to?.name || "—"} · ${tEnum(t, "mode", assign.mode)}` : ""}</SheetDescription>
          </SheetHeader>
          {assign && (
            <div className="px-4 pb-4">
              <PanelBoundary message={t("shipments.panel_unavailable")}>
                <Suspense fallback={<LoadingRows rows={4} />}>
                  <CarrierSelectionPanel shipment={shipment} shipmentId={shipment.id} leg={assign}
                    onAssigned={() => { setAssign(null); onChanged?.(); }} />
                </Suspense>
              </PanelBoundary>
            </div>
          )}
        </SheetContent>
      </Sheet>
    </>
  );
}

const toLocalInput = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fromLocalInput = (v) => (v ? new Date(v).toISOString() : null);

function EditLegDialog({ shipment, leg, onClose, onSaved }) {
  const { t } = useT();
  const [f, setF] = useState({
    carrier: leg.carrier || "", vessel: leg.vessel?.name || "", imo: leg.vessel?.imo || "", mmsi: leg.vessel?.mmsi || "",
    voyage: leg.voyage || "", flight: leg.flight || "", train: leg.train?.number || "", truck_plate: leg.truck_plate || "",
    transport_doc_no: leg.transport_doc_no || "", container_numbers: (leg.container_numbers || []).join(", "),
    etd: toLocalInput(leg.etd), eta: toLocalInput(leg.eta),
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));

  const save = async (e) => {
    e.preventDefault();
    const body = {};
    const str = (v) => (v || "").trim() || null;
    if (str(f.carrier) !== (leg.carrier || null)) body.carrier = str(f.carrier);
    if (leg.mode === "sea" && (str(f.vessel) !== (leg.vessel?.name || null) || str(f.imo) !== (leg.vessel?.imo || null) || str(f.mmsi) !== (leg.vessel?.mmsi || null))) {
      body.vessel = str(f.vessel) || str(f.imo) || str(f.mmsi) ? { name: str(f.vessel), imo: str(f.imo), mmsi: str(f.mmsi) } : null;
    }
    if (leg.mode === "sea" && str(f.voyage) !== (leg.voyage || null)) body.voyage = str(f.voyage);
    if (leg.mode === "air" && str(f.flight) !== (leg.flight || null)) body.flight = str(f.flight);
    if (leg.mode === "rail" && str(f.train) !== (leg.train?.number || null)) body.train = str(f.train) ? { ...(leg.train || {}), number: str(f.train) } : null;
    if (leg.mode === "road" && str(f.truck_plate) !== (leg.truck_plate || null)) body.truck_plate = str(f.truck_plate);
    if (str(f.transport_doc_no) !== (leg.transport_doc_no || null)) body.transport_doc_no = str(f.transport_doc_no);
    const conts = f.container_numbers.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean);
    if (conts.join(",") !== (leg.container_numbers || []).join(",")) body.container_numbers = conts;
    if (f.etd !== toLocalInput(leg.etd) && f.etd) body.etd = fromLocalInput(f.etd);
    if (f.eta !== toLocalInput(leg.eta) && f.eta) body.eta = fromLocalInput(f.eta);
    if (Object.keys(body).length === 0) { onClose(); return; }
    setBusy(true);
    setError(null);
    try {
      await apiPatch(`/shipments/${shipment.id}/legs/${leg.seq}`, body);
      onSaved();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const F = ({ k, label, hint, type = "text", className }) => (
    <div className={cn("flex flex-col gap-1", className)}>
      <Label htmlFor={`leg-${k}`}>{label}</Label>
      <Input id={`leg-${k}`} type={type} value={f[k]} onChange={set(k)} />
      {hint && <p className="text-xs text-ink-tertiary">{hint}</p>}
    </div>
  );

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("shipments.leg.edit")} {leg.seq}</DialogTitle>
          <DialogDescription>{leg.from?.name} → {leg.to?.name} · {tEnum(t, "mode", leg.mode)}</DialogDescription>
        </DialogHeader>
        <form onSubmit={save} className="grid gap-3 sm:grid-cols-2">
          {F({ k: "carrier", label: t("shipments.form.carrier"), className: "sm:col-span-2" })}
          {leg.mode === "sea" && (
            <>
              {F({ k: "vessel", label: t("shipments.form.vessel") })}
              {F({ k: "voyage", label: t("shipments.form.voyage") })}
              {F({ k: "imo", label: t("shipments.form.imo") })}
              {F({ k: "mmsi", label: t("shipments.form.mmsi"), hint: t("shipments.form.mmsi_hint") })}
            </>
          )}
          {leg.mode === "air" && F({ k: "flight", label: t("shipments.form.flight") })}
          {leg.mode === "rail" && F({ k: "train", label: t("shipments.form.train") })}
          {leg.mode === "road" && F({ k: "truck_plate", label: t("shipments.form.truck_plate") })}
          {F({ k: "transport_doc_no", label: t("shipments.form.transport_doc") })}
          {(leg.mode === "sea" || leg.mode === "rail" || leg.mode === "road") && F({ k: "container_numbers", label: t("shipments.form.containers"), hint: t("shipments.form.containers_hint"), className: "sm:col-span-2" })}
          {F({ k: "etd", label: t("shipments.form.etd"), type: "datetime-local" })}
          {F({ k: "eta", label: t("shipments.form.eta"), type: "datetime-local", hint: t("shipments.form.eta_hint") })}
          {error && <ErrorState className="sm:col-span-2" error={error} compact />}
          <DialogFooter className="sm:col-span-2">
            <Button variant="outline" onClick={onClose}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={busy}>{busy ? t("state.saving") : t("action.save")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
