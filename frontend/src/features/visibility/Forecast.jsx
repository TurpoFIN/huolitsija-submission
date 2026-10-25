import React from "react";
import { useT } from "@/lib/i18n";
import { dateTime } from "@/features/shipments/common";

export default function Forecast({ prediction: p }) {
  const { locale } = useT();
  const fi = locale === "fi";
  if (!p || p.state !== "predicted") return <p className="text-sm text-ink-tertiary">{fi ? "Etapin ennustetta ei ole saatavilla. Tuore sijainti ja nopeus tarvitaan." : "Leg forecast unavailable. A fresh position and speed are required."}</p>;
  const i = p.inputs || {};
  return <div className="space-y-2 text-sm">
    <p>{fi ? "Nykyisen etapin saapuminen" : "Current leg arrival"}: <span className="tabular">{dateTime(p.eta, locale)}</span></p>
    <p className="text-ink-secondary">{fi ? "Vaihteluväli" : "Range"}: {dateTime(p.earliest, locale)} – {dateTime(p.latest, locale)}</p>
    <p className="text-ink-tertiary">{fi ? "Rajallinen varmuus. Vakionopeusmalli, ei tilastollisesti kalibroitu." : "Limited confidence. Constant-speed model, not statistically calibrated."}</p>
    <details><summary className="cursor-pointer">{fi ? "Laskennan lähtötiedot" : "Forecast inputs"}</summary>
      <dl className="mt-2 grid grid-cols-2 gap-2">
        <dt>{fi ? "Jäljellä" : "Remaining"}</dt><dd>{i.distance_km} km</dd>
        <dt>{fi ? "Nopeus" : "Speed"}</dt><dd>{i.speed_kmh} km/h</dd>
        <dt>{fi ? "Havainto" : "Observation"}</dt><dd>{i.position?.source} · {dateTime(i.position?.at, locale)}</dd>
        <dt>{fi ? "Satamaviive" : "Port delay"}</dt><dd>{i.delay_applied_h == null ? (fi ? "Ei saatavilla" : "Unavailable") : `${i.delay_applied_h} h`}</dd>
      </dl>
    </details>
  </div>;
}
