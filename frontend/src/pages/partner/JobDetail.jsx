// Partner portal: one assigned job. The haulier accepts its legs, reports milestones with the real time, uploads the
// signed POD and reports GPS positions. Data: GET /api/partner/shipments/:id (no prices, no customs data).
import React, { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { toast } from "sonner";
import { AlertTriangle, CheckCircle2, LocateFixed, MapPin } from "lucide-react";
import { KV, PageHeader, Section } from "@/components/app/layout";
import { ErrorState, Loadable, LoadingRows } from "@/components/app/states";
import { FileUpload } from "@/components/app/FileUpload";
import { StatusBadge } from "@/components/app/StatusBadge";
import { Timeline } from "@/components/app/Timeline";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { apiPost, apiUpload, useApi } from "@/lib/api";
import { cbm, dateShort, dateTime, kg, num } from "@/lib/format";
import { tEnum } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import ShipmentMap from "@/features/map/ShipmentMap";
import { DataAge, holdLabel, ModeIcon, msLabel, shortPlace, sourceLabel, timeHM } from "@/features/shipments/common";
import { usePT } from "@/features/shipments/partnerI18n";

const EUROPE = new Set(("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE NO IS LI CH GB TR RS BA ME MK AL MD GE").split(" "));

function localInput(d = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function addressLines(p) {
  const a = p?.address || {};
  return [a.street, [a.postal_code, a.city].filter(Boolean).join(" "), a.country].filter(Boolean);
}

function When({ planned, actual, locale, pt }) {
  return (
    <span className="flex flex-col leading-tight">
      <span className={cn("tabular", !actual && "text-ink-secondary")}>{actual ? dateTime(actual, locale) : planned ? dateTime(planned, locale) : "—"}</span>
      <span className="text-xs text-ink-tertiary">
        {actual ? `${pt("p.actual")} · ${pt("p.planned").toLowerCase()} ${planned ? `${dateShort(planned, locale)} ${timeHM(planned, locale)}` : "—"}` : pt("p.estimate")}
      </span>
    </span>
  );
}

function errMsg(e, t) {
  return e?.message || t("state.error");
}

/* ---------------------------------------------------------------- accept */
function AcceptDialog({ open, onOpenChange, job, leg, onDone, pt, t }) {
  const [plate, setPlate] = useState(leg.truck_plate || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await apiPost(`/partner/shipments/${job.id}/legs/${leg.seq}/accept`, { truck_plate: plate || null });
      onDone(out);
      toast.success(pt("p.accepted", { time: "" }).trim());
      onOpenChange(false);
    } catch (e) {
      setError(errMsg(e, t));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{pt("p.accept_title")}</DialogTitle>
          <DialogDescription>{pt("p.accept_body")}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-1.5">
          <Label htmlFor="plate">{pt("p.truck_plate")} <span className="text-ink-tertiary">({pt("p.optional")})</span></Label>
          <Input id="plate" value={plate} onChange={(e) => setPlate(e.target.value.toUpperCase())} maxLength={20} placeholder="ABC-123" />
        </div>
        {error && <ErrorState error={{ message: error }} compact />}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t("action.cancel")}</Button>
          <Button onClick={submit} disabled={busy}>{pt("p.accept")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ---------------------------------------------------------------- milestone */
function MilestoneDialog({ action, onOpenChange, job, leg, isLast, onDone, pt, t }) {
  const [at, setAt] = useState(localInput());
  const [note, setNote] = useState("");
  const [file, setFile] = useState(null);
  const [signedBy, setSignedBy] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const needsPod = action === "delivered" && isLast && !(job.pods || []).length;
  const submit = async () => {
    const when = new Date(at);
    if (Number.isNaN(when.getTime()) || when.getTime() > Date.now() + 5 * 60000) {
      setError(pt("p.future"));
      return;
    }
    if (needsPod && !file) {
      setError(pt("p.pod_needed"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (file) {
        const fd = new FormData();
        fd.append("file", file);
        fd.append("signed_by", signedBy);
        fd.append("note", note);
        await apiUpload(`/partner/shipments/${job.id}/legs/${leg.seq}/pod`, fd);
      }
      let out = null;
      // a POD on the last leg already confirms delivery; otherwise record the milestone with the given time
      if (!(file && action === "delivered" && isLast)) {
        out = await apiPost(`/partner/shipments/${job.id}/legs/${leg.seq}/milestones`, { action, at: when.toISOString(), note: note || null });
      }
      onDone(out);
      toast.success(pt("p.sent", { action: pt(`p.action.${action}`) }));
      onOpenChange(false);
    } catch (e) {
      setError(errMsg(e, t));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={!!action} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{pt(`p.action.${action}`)}</DialogTitle>
          <DialogDescription>{pt(`p.action_hint.${action}`)}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="ms-at">{pt("p.when")}</Label>
            <Input id="ms-at" type="datetime-local" value={at} max={localInput()} onChange={(e) => setAt(e.target.value)} className="tabular" />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="ms-note">{pt("p.note")} <span className="text-ink-tertiary">({pt("p.optional")})</span></Label>
            <Textarea id="ms-note" rows={2} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder={pt("p.note_ph")} />
          </div>
          {action === "delivered" && (
            <div className="grid gap-1.5">
              <Label>{pt("p.pod")} {!needsPod && <span className="text-ink-tertiary">({pt("p.optional")})</span>}</Label>
              <FileUpload accept=".pdf,.jpg,.jpeg,.png,.webp,.heic" onFiles={(f) => setFile(f[0])} label={pt("p.pod_upload")} hint={pt("p.pod_hint")} />
              <Input value={signedBy} onChange={(e) => setSignedBy(e.target.value)} placeholder={pt("p.pod_signed_by")} aria-label={pt("p.pod_signed_by")} />
            </div>
          )}
        </div>
        {error && <ErrorState error={{ message: error }} compact />}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t("action.cancel")}</Button>
          <Button onClick={submit} disabled={busy}>{pt("p.send")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/* ---------------------------------------------------------------- position */
function PositionReport({ job, leg, onDone, pt, locale }) {
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const send = async (la, lo, accuracy) => {
    setBusy(true);
    setMsg(null);
    try {
      const out = await apiPost(`/partner/shipments/${job.id}/legs/${leg.seq}/position`, { lat: Number(la), lon: Number(lo), accuracy_m: accuracy ?? null });
      setMsg({ ok: true, text: pt("p.position_sent", { km: num(out.off_lane_km ?? 0, locale, 1) }) });
      onDone();
    } catch (e) {
      setMsg({ ok: false, text: e?.message });
    } finally {
      setBusy(false);
    }
  };
  const useMine = () => {
    if (!navigator.geolocation) {
      setMsg({ ok: false, text: pt("p.position_unavailable") });
      return;
    }
    setBusy(true);
    navigator.geolocation.getCurrentPosition(
      (p) => { setLat(p.coords.latitude.toFixed(5)); setLon(p.coords.longitude.toFixed(5)); send(p.coords.latitude, p.coords.longitude, p.coords.accuracy); },
      (err) => { setBusy(false); setMsg({ ok: false, text: err.code === 1 ? pt("p.position_denied") : pt("p.position_unavailable") }); },
      { enableHighAccuracy: true, timeout: 15000 },
    );
  };
  const valid = lat !== "" && lon !== "" && Math.abs(Number(lat)) <= 90 && Math.abs(Number(lon)) <= 180;
  return (
    <div className="mt-3 rounded-lg bg-secondary/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-medium">{pt("p.position")}</p>
          <p className="text-xs text-ink-tertiary">
            {leg.last_report ? pt("p.last_report", { time: `${dateShort(leg.last_report.at, locale)} ${timeHM(leg.last_report.at, locale)}` }) : pt("p.position_hint")}
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={useMine} disabled={busy}><LocateFixed /> {pt("p.position_use")}</Button>
      </div>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <div className="grid gap-1">
          <Label htmlFor={`lat-${leg.seq}`} className="text-xs">{pt("p.lat")}</Label>
          <Input id={`lat-${leg.seq}`} inputMode="decimal" value={lat} onChange={(e) => setLat(e.target.value)} className="h-8 w-32 tabular" placeholder="60.21502" />
        </div>
        <div className="grid gap-1">
          <Label htmlFor={`lon-${leg.seq}`} className="text-xs">{pt("p.lon")}</Label>
          <Input id={`lon-${leg.seq}`} inputMode="decimal" value={lon} onChange={(e) => setLon(e.target.value)} className="h-8 w-32 tabular" placeholder="25.19392" />
        </div>
        <Button size="sm" variant="secondary" disabled={!valid || busy} onClick={() => send(lat, lon)}><MapPin /> {pt("p.position_send")}</Button>
      </div>
      {msg && <p className={cn("mt-2 text-xs", msg.ok ? "text-ink-secondary" : "text-danger-fg")} role="status">{msg.text}</p>}
    </div>
  );
}

/* ---------------------------------------------------------------- leg card */
function LegCard({ job, leg, isLast, onChanged, pt, t, locale }) {
  const [accepting, setAccepting] = useState(false);
  const [action, setAction] = useState(null);
  const done = !!leg.ata || job.status === "delivered";
  const accepted = !!leg.accepted_at;
  const canAct = !done && job.status !== "cancelled";
  const actions = !leg.atd ? ["picked_up"] : isLast ? ["arrived", "delivered"] : ["arrived"];
  return (
    <div className="rounded-xl border border-[color:var(--border-subtle)] p-3 sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <ModeIcon mode={leg.mode} colored />
          <span className="text-sm font-medium">{pt("p.leg_n", { n: leg.seq })}</span>
          <span className="text-sm text-ink-secondary">{pt(`p.role.${leg.role || "main"}`)}</span>
          {leg.service && <span className="truncate text-xs text-ink-tertiary">· {leg.service}</span>}
        </div>
        {done ? (
          <span className="inline-flex items-center gap-1 text-xs text-ink-secondary"><CheckCircle2 className="size-3.5" /> {pt("p.leg_done")}</span>
        ) : accepted ? (
          <span className="text-xs text-ink-tertiary">{pt("p.accepted", { time: `${dateShort(leg.accepted_at, locale)} ${timeHM(leg.accepted_at, locale)}` })}</span>
        ) : canAct && !leg.atd ? (
          <Button size="sm" onClick={() => setAccepting(true)}>{pt("p.accept")}</Button>
        ) : null}
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div className="min-w-0">
          <p className="text-xs text-ink-tertiary">{pt("p.departure")}</p>
          <p className="truncate text-sm font-medium">{shortPlace(leg.from)}</p>
          {(addressLines(leg.from).join(", ") || (leg.from?.name !== shortPlace(leg.from) ? leg.from?.name : "")) && (
            <p className="text-xs text-ink-secondary">{addressLines(leg.from).join(", ") || leg.from?.name}</p>
          )}
          <div className="mt-1 text-sm"><When planned={leg.etd} actual={leg.atd} locale={locale} pt={pt} /></div>
        </div>
        <div className="min-w-0">
          <p className="text-xs text-ink-tertiary">{pt("p.arrival")}</p>
          <p className="truncate text-sm font-medium">{shortPlace(leg.to)}</p>
          {(addressLines(leg.to).join(", ") || (leg.to?.name !== shortPlace(leg.to) ? leg.to?.name : "")) && (
            <p className="text-xs text-ink-secondary">{addressLines(leg.to).join(", ") || leg.to?.name}</p>
          )}
          <div className="mt-1 text-sm"><When planned={leg.eta} actual={leg.ata} locale={locale} pt={pt} /></div>
        </div>
      </div>

      <KV className="mt-3 grid-cols-3" cols={3} items={[
        { label: pt("p.plate"), value: leg.truck_plate, mono: true },
        { label: pt("p.doc_no"), value: leg.transport_doc_no, mono: true },
        { label: pt("p.distance"), value: leg.distance_km ? `${num(leg.distance_km, locale)} km` : null },
      ]} />

      {canAct && (accepted || leg.atd) && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-[color:var(--border-subtle)] pt-3">
          <span className="mr-1 text-xs text-ink-tertiary">{pt("p.report")}</span>
          {actions.map((a) => (
            <Button key={a} size="sm" variant={a === leg.next_action || (a === "delivered" && leg.next_action === "delivered") ? "default" : "outline"} onClick={() => setAction(a)}>
              {pt(`p.action.${a}`)}
            </Button>
          ))}
        </div>
      )}
      {canAct && leg.atd && !leg.ata && leg.mode === "road" && <PositionReport job={job} leg={leg} onDone={onChanged} pt={pt} locale={locale} />}

      {accepting && <AcceptDialog open={accepting} onOpenChange={setAccepting} job={job} leg={leg} onDone={onChanged} pt={pt} t={t} />}
      {action && <MilestoneDialog action={action} onOpenChange={(o) => !o && setAction(null)} job={job} leg={leg} isLast={isLast} onDone={onChanged} pt={pt} t={t} />}
    </div>
  );
}

function Party({ title, p, pt }) {
  if (!p) return null;
  return (
    <div className="min-w-0">
      <p className="text-xs text-ink-tertiary">{title}</p>
      <p className="text-sm font-medium">{p.name || "—"}</p>
      {addressLines(p).map((l) => <p key={l} className="text-sm text-ink-secondary">{l}</p>)}
      <KV className="mt-2" cols={1} items={[
        p.contact_name && { label: pt("p.contact"), value: p.contact_name },
        p.phone && { label: pt("p.phone"), value: <a className="text-ink-accent hover:underline" href={`tel:${p.phone}`}>{p.phone}</a> },
        p.opening_hours && { label: pt("p.hours"), value: p.opening_hours },
        p.instructions && { label: pt("p.instructions"), value: p.instructions },
      ]} />
    </div>
  );
}

/* ---------------------------------------------------------------- page */
export default function JobDetail() {
  const { id } = useParams();
  const { pt, t, locale } = usePT();
  const q = useApi(`/partner/shipments/${id}`, { pollMs: 60000 });
  const job = q.data;
  const lastSeq = useMemo(() => {
    if (!job) return null;
    return Math.max(...[...(job.legs || []), ...(job.other_legs || [])].map((l) => l.seq));
  }, [job]);
  const intercontinental = useMemo(() => {
    if (!job) return false;
    return [...(job.legs || []), ...(job.other_legs || [])].some((l) => [l.from?.country, l.to?.country].some((c) => c && !EUROPE.has(c)));
  }, [job]);
  const onChanged = (out) => {
    if (out && out.id === id) q.setData(out);
    q.reload();
  };
  const first = job?.legs?.[0];
  const last = job?.legs?.[job.legs.length - 1];

  return (
    <div className="mx-auto w-full max-w-6xl">
      <Loadable q={q} skeleton={<LoadingRows rows={8} />}>
        {(j) => (
          <>
            <PageHeader
              crumbs={[{ label: pt("p.back"), to: "/partner" }, { label: j.reference }]}
              title={<span className="font-mono">{j.reference}</span>}
              meta={
                <>
                  <StatusBadge group="status" value={j.status} />
                  <span className="truncate">{shortPlace(first?.from)} <span className="text-ink-tertiary">→</span> {shortPlace(last?.to)}</span>
                  {last?.eta && !last?.ata && <span className="tabular text-ink-tertiary">ETA {dateShort(last.eta, locale)} {timeHM(last.eta, locale)} ({pt("p.estimate")})</span>}
                </>
              }
              actions={<DataAge q={q} />}
            />
            {j.holds?.length > 0 && (
              <div role="alert" className="mb-4 flex items-start gap-2 rounded-lg px-3 py-2 text-sm status-pending">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                {pt("p.holds", { types: j.holds.map((h) => holdLabel(t, h.type)).join(", ") })}
              </div>
            )}
            <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
              <Section title={pt("p.your_legs")} className="min-w-0">
                <div className="grid gap-3">
                  {j.legs.map((lg) => (
                    <LegCard key={lg.seq} job={j} leg={lg} isLast={lg.seq === lastSeq} onChanged={onChanged} pt={pt} t={t} locale={locale} />
                  ))}
                </div>
              </Section>
              <Section title={pt("p.parties")} className="min-w-0">
                <div className="grid gap-4">
                  <Party title={pt("p.shipper")} p={j.parties?.shipper} pt={pt} />
                  <Party title={pt("p.consignee")} p={j.parties?.consignee} pt={pt} />
                  {!j.parties?.shipper && !j.parties?.consignee && <p className="text-sm text-ink-tertiary">—</p>}
                </div>
              </Section>
            </div>
            <Section title={pt("p.map")}>
              <ShipmentMap embedded scope="partner" initialSelectedId={j.id} height={360} initialView={intercontinental ? "world" : "europe"} />
            </Section>
            <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
              <div className="min-w-0">
                <Section title={pt("p.milestones")}>
                  <Timeline events={(j.milestones || []).map((m, i) => ({
                    id: `${m.code}-${m.leg_seq}-${i}`, at: m.at || m.planned, title: msLabel(t, m), future: !m.at, estimated: !m.at,
                    source: m.at ? sourceLabel(t, m.source) : pt("p.estimate"),
                  }))} />
                </Section>
                {(j.pods || []).length > 0 && (
                  <Section title={pt("p.pods")}>
                    <ul className="text-sm">
                      {j.pods.map((p) => (
                        <li key={p.id} className="flex flex-wrap items-center justify-between gap-2 py-1">
                          <span className="truncate">{p.filename}</span>
                          <span className="text-xs text-ink-tertiary tabular">{p.signed_by ? `${p.signed_by} · ` : ""}{dateTime(p.created_at, locale)}</span>
                        </li>
                      ))}
                    </ul>
                  </Section>
                )}
              </div>
              <aside className="min-w-0">
                <Section title={pt("p.cargo")}>
                  <p className="mb-2 text-sm">{j.cargo?.description || "—"}</p>
                  <KV cols={2} className="grid-cols-2" items={[
                    { label: pt("p.packages"), value: j.cargo?.packages ? `${num(j.cargo.packages, locale)} ${tEnum(t, "package_type", j.cargo.package_type)}` : null },
                    { label: pt("p.weight"), value: j.cargo?.weight_kg ? kg(j.cargo.weight_kg, locale) : null },
                    { label: pt("p.volume"), value: j.cargo?.volume_m3 ? cbm(j.cargo.volume_m3, locale) : null },
                    { label: pt("p.ldm"), value: j.cargo?.ldm ? num(j.cargo.ldm, locale, 1) : null },
                    j.cargo?.containers?.length ? { label: pt("p.containers"), value: j.cargo.containers.map((c) => `${c.count || 1}×${c.type}`).join(", ") } : null,
                    { label: pt("p.dg"), value: j.cargo?.dangerous_goods ? `${pt("p.yes")}${j.cargo?.dg?.un ? ` · ${j.cargo.dg.un}` : ""}` : pt("p.no") },
                  ]} />
                </Section>
                <Section title={pt("p.refs")}>
                  <KV cols={1} items={[
                    { label: pt("p.ref"), value: j.references?.reference, mono: true },
                    { label: pt("p.customer_ref"), value: j.references?.customer_ref, mono: true },
                    { label: pt("p.po"), value: (j.references?.po_numbers || []).join(", ") || null, mono: true },
                    { label: pt("p.doc_no"), value: (j.references?.transport_doc_nos || []).join(", ") || null, mono: true },
                  ]} />
                </Section>
                {(j.other_legs || []).length > 0 && (
                  <Section title={pt("p.other_legs")}>
                    <p className="mb-2 text-xs text-ink-tertiary">{pt("p.other_legs_hint")}</p>
                    <ul className="grid gap-2">
                      {j.other_legs.map((l) => (
                        <li key={l.seq} className="flex items-start gap-2 text-sm">
                          <ModeIcon mode={l.mode} className="mt-0.5" />
                          <div className="min-w-0 flex-1">
                            <p className="truncate">{shortPlace(l.from)} <span className="text-ink-tertiary">→</span> {shortPlace(l.to)}</p>
                            <p className="text-xs text-ink-tertiary tabular">
                              {tEnum(t, "mode", l.mode)} · {l.ata ? `${pt("p.actual")} ${dateShort(l.ata, locale)}` : `ETA ${dateShort(l.eta, locale)}`}
                            </p>
                          </div>
                        </li>
                      ))}
                    </ul>
                  </Section>
                )}
              </aside>
            </div>
          </>
        )}
      </Loadable>
    </div>
  );
}
