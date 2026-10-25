import React, { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import ShipmentMap from "@/features/map/ShipmentMap";
import { useT } from "@/lib/i18n";
import { apiGet } from "@/lib/api";

export default function MapPage() {
  const { t, locale } = useT();
  const [params] = useSearchParams();
  const [observations, setObservations] = useState([]);
  const [focus, setFocus] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const refresh = () => Promise.all([apiGet("/visibility/vessels", {signal: controller.signal}), apiGet("/visibility/flights", {signal: controller.signal})]).then(([v, f]) => {
      if (controller.signal.aborted) return;
      setObservations([...(v.positions || []).map(x => ({...x, id: `v:${x.mmsi}`, kind: "vessel", label: x.name || `MMSI ${x.mmsi}`})), ...(f.positions || []).map(x => ({...x, id: `f:${x.icao24}`, kind: "flight", label: x.callsign || x.icao24}))]);
      setFailed(false);
    }).catch(e => { if (e.name !== "AbortError") setFailed(true); });
    refresh(); const timer = setInterval(refresh, 30000);
    return () => { controller.abort(); clearInterval(timer); };
  }, []);
  const selected = observations.find(x => x.id === focus);
  const fi = locale === "fi";
  return <div>
    <h1 className="sr-only">{t("nav.map")}</h1>
    <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-ink-secondary">
      <label htmlFor="visibility-focus">{fi ? "Syötteen havainto" : "Feed observation"}</label>
      <select id="visibility-focus" value={focus} onChange={e => setFocus(e.target.value)} className="max-w-full rounded border border-[color:var(--border-default)] bg-[var(--bg-surface)] px-2 py-1">
        <option value="">{fi ? "Valitse alus tai lento" : "Choose vessel or flight"}</option>
        {observations.map(o => <option key={o.id} value={o.id}>{o.label} · {o.source}</option>)}
      </select>
      {selected && <span>{selected.lat.toFixed(3)}, {selected.lon.toFixed(3)} · {new Date(selected.at).toLocaleString(fi ? "fi-FI" : "en-GB")}</span>}
      <span>{failed ? (fi ? "Syötteen päivitys epäonnistui" : "Feed refresh failed") : !observations.length ? (fi ? "Ei tuoreita syötehavaintoja" : "No fresh feed observations") : (fi ? "Havainto ei vahvista lastin liikettä" : "Observation does not confirm cargo movement")}</span>
    </div>
    <ShipmentMap scope="ops" height="calc(100dvh - 148px)" observations={observations} observationFocus={focus} initialSelectedId={params.get("select") || undefined} initialView={params.get("view") || undefined} />
  </div>;
}
