import React, { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiPost, useApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { NativeSelect } from "./common";

const words = {
  fi: { title: "Julkiset kuljetuslisät", intro: "Laske kuljetusliikkeen tai terminaalin julkaisema lisämaksu. Rahti, kapasiteetti ja varausvahvistus eivät sisälly laskelmaan.", tariff: "Julkaistu lisämaksu", choose: "Valitse lisämaksu", origin: "Lähtöpaikan UN/LOCODE", destination: "Määräpaikan UN/LOCODE", date: "Palvelupäivä", pcd: "Kuljetusliikkeen hinnanlaskentapäivä (PCD)", booking: "Varauksen päivä", pcdHelp: "FMC:n ei-SPOT-varaus: viimeinen kontin portille saapumispäivä. SPOT: ensimmäisen aluksen ETD.", metres: "Kaistametrit (m)", kg: "Veloituspaino (kg)", amount: "CAF-lisän sopimuksen mukainen laskentapohja (INR)", cafHelp: "Julkaisu kertoo prosenttiosuuden. Varmista laskentapohja kuljetusliikkeen sopimuksesta.", count: "Konttien määrä", equipment: "Konttityyppi", calculate: "Laske lisämaksu", calculating: "Lasketaan…", source: "Avaa alkuperäinen tariffi", valid: "Voimassa alkaen", until: "asti", checked: "Lähde haettu", taxes: "Verot ja muut maksut eivät sisälly.", failed: "Lähdettä ei voitu tarkistaa. Laskenta ei ole käytettävissä.", retry: "Yritä uudelleen", loading: "Haetaan kuljetusliikkeiden tariffit…", empty: "Julkaistuja tariffitietoja ei saatu.", result: "Lisämaksu", noMatch: "Tariffi ei koske valittua reittiä, palvelua tai kalustoa.", notYet: "Lisämaksu ei ole vielä voimassa valittuna päivänä.", expired: "Valitulle päivälle ei ole voimassa olevaa tariffia.", missing: "Tarkista tarvittavat tiedot ja hinnanlaskentapäivä.", scope: "Soveltamisala", excluded: "Rajattu pois", services: {AIR:"Lento",RORO:"Ro-ro",FCL:"Täyskontti"}, untilNotice: "toistaiseksi", limits: "Vähintään / enintään", base: "Yksikköhinta", perkg:"/ kg", permetre:"/ m", disclaimer: "Laskelma ei ole rahdin tarjous eikä varausvahvistus." },
  en: { title: "Published transport surcharges", intro: "Calculate a carrier or terminal's published surcharge. Freight, capacity and booking confirmation are separate.", tariff: "Published surcharge", choose: "Choose a surcharge", origin: "Origin UN/LOCODE", destination: "Destination UN/LOCODE", date: "Service date", pcd: "Carrier price calculation date (PCD)", booking: "Booking date", pcdHelp: "FMC non-SPOT: last container gate-in date. SPOT: first vessel ETD.", metres: "Lane metres (m)", kg: "Chargeable weight (kg)", amount: "Contractual CAF-subject amount (INR)", cafHelp: "The publication gives the percentage. Confirm the contractual base with the carrier.", count: "Container count", equipment: "Equipment", calculate: "Calculate surcharge", calculating: "Calculating…", source: "Open original tariff", valid: "Effective from", until: "to", checked: "Source retrieved", taxes: "Taxes and other charges excluded.", failed: "The provider source could not be verified. Calculation is unavailable.", retry: "Retry", loading: "Fetching carrier tariffs…", empty: "No published tariffs could be retrieved.", result: "Surcharge", noMatch: "The tariff does not apply to this lane, service or equipment.", notYet: "This surcharge is not yet effective on the selected date.", expired: "No valid tariff is available for the selected date.", missing: "Check the required inputs and price calculation date.", scope: "Scope", excluded: "Excluded", services: {AIR:"Air",RORO:"Ro-ro",FCL:"Full container"}, untilNotice: "until further notice", limits: "Minimum / maximum", base: "Unit rate", perkg:"/ kg", permetre:"/ m", disclaimer: "This calculation is neither a freight offer nor a booking confirmation." },
};

export default function PublicSurcharges() {
  const { locale } = useT();
  const w = words[locale === "fi" ? "fi" : "en"];
  const [open, setOpen] = useState(false);
  const q = useApi("/public/surcharges", { enabled: open });
  const [id, setId] = useState("");
  const [form, setForm] = useState({});
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const items = q.data?.items || [];
  const tariff = items.find((x) => x.id === id);
  const update = (key, value) => { setForm((v) => ({ ...v, [key]: value })); setResult(null); setError(""); };
  const choose = (value) => { setId(value); setForm({}); setResult(null); setError(""); };
  const field = (key, label, type = "text") => <label className="flex min-w-0 flex-col gap-1 text-xs">{label}<Input aria-label={label} required type={type} value={form[key] ?? ""} min={type === "number" ? "0.001" : undefined} step="any" maxLength={type === "text" ? 5 : undefined} pattern={type === "text" ? "[A-Z]{2}[A-Z0-9]{3}" : undefined} onChange={(e) => update(key, type === "text" ? e.target.value.toUpperCase() : e.target.value)} /></label>;
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setError(""); setResult(null);
    const body = { tariff_id: id, provider: tariff.provider, service: tariff.scope.service,
      origin: form.origin, destination: form.destination, on_date: form.on_date,
      date_basis: tariff.scope.date_basis || "service_date" };
    if (tariff.basis === "PER_CONTAINER") { body.equipment = form.equipment; body.containers = Number(form.containers); }
    if (tariff.basis === "PER_KG") body.chargeable_kg = form.chargeable_kg;
    if (tariff.basis === "PER_LDM") body.lane_metres = form.lane_metres;
    if (tariff.basis === "PERCENT") { body.caf_subject_amount = form.caf_subject_amount; body.base_currency = "INR"; }
    try { setResult(await apiPost("/public/surcharges/calculate", body)); }
    catch (e2) { setError(e2.message); }
    finally { setBusy(false); }
  };
  const resultText = result?.reason === "not_yet_effective" ? w.notYet : result?.reason === "tariff_period_expired" ? w.expired : result?.state === "needs_input" ? w.missing : result?.state === "unavailable" ? w.failed : w.noMatch;
  const scope = tariff?.scope;
  return <details className="mb-4 border-b border-line pb-3" onToggle={(e) => setOpen(e.currentTarget.open)}>
    <summary className="cursor-pointer py-2 text-sm">{w.title}</summary>
    <p className="mb-3 max-w-3xl text-xs text-ink-secondary">{w.intro}</p>
    {q.loading && <p role="status" className="text-sm">{w.loading}</p>}
    {q.error && <p role="alert" className="text-sm">{w.failed} <Button size="sm" variant="outline" onClick={q.reload}>{w.retry}</Button></p>}
    {(q.data?.sources || []).filter((x) => x.state !== "available").map((x) => <p key={x.provider} className="mb-2 text-xs text-warning-fg">{x.provider}: {w.failed}</p>)}
    {q.data && !items.length && <p className="text-sm">{w.empty}</p>}
    {items.length > 0 && <form onSubmit={submit} className="max-w-4xl">
      <fieldset disabled={busy} className="space-y-3">
        <label className="flex flex-col gap-1 text-xs">{w.tariff}<NativeSelect aria-label={w.tariff} value={id} placeholder={w.choose} onChange={choose} options={items.map((x) => ({ value: x.id, label: `${x.code} · ${x.label}` }))} /></label>
        {tariff && <>
          <div className="space-y-1 text-xs text-ink-secondary">
            <p>{w.scope}: {w.services[scope.service]} · {(scope.port_pair || scope.either_ports || [scope.origin || scope.origin_country || scope.origin_countries?.join(", "), scope.destination || scope.destination_country || scope.destination_countries?.join(", ")]).filter(Boolean).join(" · ")}</p>
            {scope.excluded_countries && <p>{w.excluded}: {scope.excluded_countries.join(", ")}</p>}
            <p>{w.valid} {tariff.effective_from} {tariff.effective_to ? `${w.until} ${tariff.effective_to}` : w.untilNotice}</p>
            <p>{w.base}: {tariff.rate ? `${tariff.rate} ${tariff.basis === "PERCENT" ? "%" : `${tariff.currency} ${tariff.basis === "PER_KG" ? w.perkg : w.permetre}`}` : Object.entries(tariff.rates).map(([k,v]) => `${k}: ${v} ${tariff.currency}`).join(" · ")}</p>
            {tariff.minimum && <p>{w.limits}: {tariff.minimum} / {tariff.maximum} {tariff.currency}</p>}
            <p><a className="text-ink-accent underline" href={tariff.source.url} target="_blank" rel="noreferrer">{w.source}</a> · {w.checked}: {new Date(tariff.source.retrieved_at).toLocaleString(locale)}</p>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {field("origin",w.origin)}{field("destination",w.destination)}
            {field("on_date",scope.date_basis === "carrier_price_calculation_date" ? w.pcd : scope.date_basis === "booking_date" ? w.booking : w.date,"date")}
            {tariff.basis === "PER_LDM" && field("lane_metres",w.metres,"number")}
            {tariff.basis === "PER_KG" && field("chargeable_kg",w.kg,"number")}
            {tariff.basis === "PERCENT" && field("caf_subject_amount",w.amount,"number")}
            {tariff.basis === "PER_CONTAINER" && <>{field("containers",w.count,"number")}<label className="flex flex-col gap-1 text-xs">{w.equipment}<NativeSelect aria-label={w.equipment} value={form.equipment || ""} placeholder={w.equipment} onChange={(v) => update("equipment",v)} options={Object.keys(tariff.rates).map((v) => ({value:v,label:v}))} /></label></>}
          </div>
          {scope.date_basis === "carrier_price_calculation_date" && <p className="text-xs text-ink-secondary">{w.pcdHelp}</p>}
          {tariff.basis === "PERCENT" && <p className="text-xs text-ink-secondary">{w.cafHelp}</p>}
          <Button type="submit" size="sm" disabled={busy || (tariff.basis === "PER_CONTAINER" && !form.equipment)}>{busy ? w.calculating : w.calculate}</Button>
        </>}
      </fieldset>
      {error && <p role="alert" className="mt-3 text-sm text-danger-fg">{error}</p>}
      {result && <div role="status" className="mt-3 space-y-1 text-sm">
        {result.state === "calculated" ? <><p className="tabular">{w.result}: {result.amount} {result.currency}</p><p className="text-xs text-ink-secondary">{w.taxes} {w.disclaimer}</p></> : <p>{resultText}</p>}
      </div>}
    </form>}
  </details>;
}
