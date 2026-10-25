// Parties (importer, seller + sanctions screening, representative) and the broker-owned filing fields:
// office of presentation, location of goods, transport document, containers, means of transport, representation.
import React, { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { KV } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { SaveState } from "./common";

const SCREEN_STATE = { clear: "success", potential_match: "failed", error: "pending" };

function addr(a) {
  if (!a) return null;
  if (typeof a === "string") return a;
  return [a.street, [a.postal_code, a.city].filter(Boolean).join(" "), a.country].filter(Boolean).join(", ");
}

function init(decl) {
  const tr = decl?.transport || {};
  return {
    office: decl?.office?.code || "",
    location_of_goods: decl?.location_of_goods || "",
    transport_document_number: tr.transport_document?.number || "",
    container_numbers: (tr.container_numbers || []).join(", "),
    means_on_arrival: tr.means_on_arrival || "",
    representation_type: decl?.representation_type || "direct",
  };
}

export function Screening({ decl, onScreen, busy }) {
  const { t, locale } = useT();
  const s = decl?.screening;
  const st = s?.status;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <StatusBadge size="sm" state={SCREEN_STATE[st] || "expired"} label={t(`customs.screening.${st || "none"}`)} />
      {s?.screened_at && <span className="text-xs text-ink-tertiary">{dateTime(s.screened_at, locale)}</span>}
      {onScreen && (
        <Button variant="ghost" size="xs" onClick={onScreen} disabled={busy}>
          <RefreshCw className={busy ? "animate-spin" : ""} /> {t("customs.action.screen")}
        </Button>
      )}
    </span>
  );
}

export default function Parties({ decl, canEdit, onPatch, saveState, onScreen, screening }) {
  const { t } = useT();
  const [f, setF] = useState(() => init(decl));
  const [err, setErr] = useState(null);
  const orig = init(decl);
  const origKey = JSON.stringify(orig);
  useEffect(() => setF(JSON.parse(origKey)), [origKey]);
  const dirty = Object.keys(f).filter((k) => f[k] !== orig[k]);
  const set = (k) => (e) => setF((s) => ({ ...s, [k]: e.target.value }));
  const imp = decl?.importer || {};
  const sel = decl?.seller || {};
  const tr = decl?.transport || {};

  const save = async () => {
    const body = {};
    if (dirty.includes("office")) body.office = { code: f.office.replace(/\s/g, "").toUpperCase() };
    if (dirty.includes("location_of_goods")) body.location_of_goods = f.location_of_goods.trim();
    if (dirty.includes("representation_type")) body.representation_type = f.representation_type;
    const trp = {};
    if (dirty.includes("transport_document_number")) trp.transport_document_number = f.transport_document_number.trim();
    if (dirty.includes("container_numbers")) trp.container_numbers = f.container_numbers.split(/[,\s]+/).map((x) => x.trim().toUpperCase()).filter(Boolean);
    if (dirty.includes("means_on_arrival")) trp.means_on_arrival = f.means_on_arrival.trim();
    if (Object.keys(trp).length) body.transport = trp;
    setErr(null);
    try { await onPatch(body, "parties"); } catch (e) { setErr(e.message); }
  };

  const lbl = "text-xs text-ink-tertiary";
  const hint = "text-xs text-ink-tertiary";
  const dis = !canEdit;
  return (
    <div className="flex flex-col gap-5">
      <KV cols={3} items={[
        { label: t("customs.party.importer"), value: imp.name, hint: addr(imp.address) },
        { label: `${t("customs.party.importer")} · ${t("customs.party.eori")}`, value: imp.eori, mono: true },
        { label: t("customs.party.representation"),
          value: `${t(`customs.rep.${decl?.representation_type || "direct"}`)} (${decl?.representative_status_code || "—"})`,
          hint: decl?.representative?.name },
        { label: t("customs.party.seller"), value: sel.name, hint: addr(sel.address) },
        { label: `${t("customs.party.seller")} · ${t("customs.party.vat_id")}`, value: sel.vat_id || sel.eori, mono: true },
        { label: "K-1", value: <Screening decl={decl} onScreen={onScreen} busy={screening} /> },
        { label: t("customs.transport.mode"), value: tr.mode ? tEnumMode(t, tr.mode) : null, hint: tr.containerised ? "FCL" : null },
        { label: t("customs.transport.port"), value: tr.port_of_arrival, mono: true },
        { label: t("customs.transport.dispatch"), value: tr.dispatch_country, mono: true },
      ]} />

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label className="flex flex-col gap-1">
          <span className={lbl}>{t("customs.transport.office")} <span className="font-mono">C-2</span></span>
          <Input value={f.office} onChange={set("office")} disabled={dis} placeholder="FI00xxxx" maxLength={8} className="font-mono uppercase" />
          <span className={hint}>{t("customs.transport.office_hint")}</span>
        </label>
        <label className="flex flex-col gap-1">
          <span className={lbl}>{t("customs.transport.location")} <span className="font-mono">C-2</span></span>
          <Input value={f.location_of_goods} onChange={set("location_of_goods")} disabled={dis} />
          <span className={hint}>{t("customs.transport.location_hint")}</span>
        </label>
        <label className="flex flex-col gap-1">
          <span className={lbl}>{t("customs.transport.doc")} <span className="font-mono">DOC-2</span></span>
          <Input value={f.transport_document_number} onChange={set("transport_document_number")} disabled={dis} className="font-mono" />
          <span className={hint}>{t("customs.transport.doc_hint")}</span>
        </label>
        <label className="flex flex-col gap-1">
          <span className={lbl}>{t("customs.transport.containers")}</span>
          <Input value={f.container_numbers} onChange={set("container_numbers")} disabled={dis} className="font-mono uppercase" />
          <span className={hint}>{t("customs.transport.containers_hint")}</span>
        </label>
        <label className="flex flex-col gap-1">
          <span className={lbl}>{t("customs.transport.means")}</span>
          <Input value={f.means_on_arrival} onChange={set("means_on_arrival")} disabled={dis} />
        </label>
        <div className="flex flex-col gap-1">
          <span className={lbl}>{t("customs.party.representation")}</span>
          <Select value={f.representation_type} onValueChange={(v) => setF((s) => ({ ...s, representation_type: v }))} disabled={dis}>
            <SelectTrigger aria-label={t("customs.party.representation")}><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="direct">{t("customs.rep.direct")} (2)</SelectItem>
              <SelectItem value="indirect">{t("customs.rep.indirect")} (3)</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      {canEdit && (
        <div className="flex items-center gap-3">
          <Button size="sm" onClick={save} disabled={!dirty.length || saveState === "saving"}>{t("action.save")}</Button>
          {dirty.length > 0 && <Button size="sm" variant="ghost" onClick={() => { setF(orig); setErr(null); }}>{t("action.cancel")}</Button>}
          {err ? <span className="text-xs text-danger-fg">{err}</span> : <SaveState state={saveState} />}
        </div>
      )}
    </div>
  );
}

function tEnumMode(t, mode) {
  const k = `enum.mode.${mode}`;
  const s = t(k);
  return s === k ? mode : s;
}
