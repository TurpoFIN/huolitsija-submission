// Facts column of the shipment detail: cargo, terms + liability, parties, references (editable), ops checks.
import React, { useState } from "react";
import { Check, Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { KV, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiPatch } from "@/lib/api";
import { cbm, kg, money, num } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { fmtKm } from "@/features/shipments/common";

/** Translation with a data fallback: unknown keys show the API's own text. */
const tr = (t, key, fallback) => {
  const v = t(key);
  return v === key ? fallback : v;
};

export function CargoFacts({ s }) {
  const { t, locale } = useT();
  const c = s.cargo || {};
  const containers = (c.containers || []).map((x) => (typeof x === "string" ? x : `${x.count || 1} × ${x.type || x.size || ""}`.trim())).join(", ");
  return (
    <Section title={t("shipments.section.cargo")}>
      <KV cols={2} items={[
        { label: t("shipments.cargo.description"), value: c.description, className: "sm:col-span-2" },
        { label: t("shipments.cargo.packages"), value: c.packages ? `${num(c.packages, locale)} ${tEnum(t, "package_type", c.package_type)}`.trim() : null },
        { label: t("shipments.cargo.weight"), value: s.weight_kg != null ? kg(s.weight_kg, locale) : null },
        { label: t("shipments.cargo.volume"), value: s.volume_m3 != null ? cbm(s.volume_m3, locale) : null },
        c.ldm != null ? { label: t("shipments.cargo.ldm"), value: `${num(c.ldm, locale, 1)} ldm` } : null,
        containers ? { label: t("shipments.cargo.containers"), value: containers } : null,
        s.teu != null ? { label: t("shipments.cargo.teu"), value: num(s.teu, locale, 1) } : null,
        { label: t("shipments.cargo.dg"), value: c.dangerous_goods || c.dg ? (c.dg?.un_number ? `${t("shipments.cargo.dg_yes")} · UN ${c.dg.un_number}` : t("shipments.cargo.dg_yes")) : t("shipments.cargo.dg_no") },
        c.hs_hint ? { label: t("shipments.cargo.hs"), value: c.hs_hint, mono: true } : null,
      ]} />
    </Section>
  );
}

export function TermsFacts({ s, ops }) {
  const { t, locale } = useT();
  const dirKey = `shipments.terms.direction.${s.direction}`;
  const ins = s.insurance || {};
  return (
    <Section title={t("shipments.section.terms")}>
      <KV cols={2} items={[
        { label: t("shipments.terms.incoterm"), value: s.incoterm ? `${s.incoterm}${s.incoterm_place ? ` ${s.incoterm_place}` : ""} (Incoterms® 2020)` : null },
        { label: t("shipments.terms.direction"), value: s.direction ? (t(dirKey) === dirKey ? s.direction : t(dirKey)) : null },
        { label: t("shipments.terms.customs"), value: s.customs_clearance == null ? null : s.customs_clearance ? t("shipments.terms.customs_yes") : t("shipments.terms.customs_no") },
        { label: t("shipments.terms.distance"), value: s.distance_km ? fmtKm(s.distance_km, locale) : null },
        { label: t("shipments.terms.liability"), value: tr(t, `shipments.liability.${s.liability_regime}`, s.liability?.label || s.liability_regime), className: "sm:col-span-2" },
        { label: t("shipments.terms.limit"), value: tr(t, `shipments.liability_limit.${s.liability_regime}`, s.liability?.limit), className: "sm:col-span-2" },
        { label: t("shipments.terms.role"), value: s.forwarder_role ? t(`shipments.terms.role.${s.forwarder_role}`) : null, className: "sm:col-span-2" },
        { label: t("shipments.terms.insurance"), value: ins.type ? `${ins.type.replace("_", " ")}${ins.insured_value_pct ? ` · ${ins.insured_value_pct} %` : ""}` : t("shipments.terms.insurance_none") },
        s.price?.total_cents != null ? {
          label: t("shipments.terms.price"),
          value: <span className="tabular">{money(s.price.total_cents, locale)}</span>,
          hint: s.price.demo_estimate ? t("shipments.terms.price_estimate") : (ops ? null : null),
        } : null,
      ]} />
    </Section>
  );
}

function addr(a) {
  if (!a) return null;
  if (typeof a === "string") return a;
  return [a.street, [a.postal_code, a.city].filter(Boolean).join(" "), a.country].filter(Boolean).join(", ");
}

export function PartyBlock({ label, p }) {
  const { t } = useT();
  return (
    <div className="min-w-0">
      <p className="text-xs text-ink-tertiary">{label}</p>
      {p ? (
        <div className="mt-0.5 text-sm">
          <p className="font-medium">{p.name || "—"}</p>
          {addr(p.address) && <p className="text-ink-secondary">{addr(p.address)}</p>}
          {(p.contact_name || p.phone || p.email) && (
            <p className="text-ink-secondary">{[p.contact_name, p.phone, p.email].filter(Boolean).join(" · ")}</p>
          )}
          {p.opening_hours && <p className="text-xs text-ink-tertiary">{t("shipments.parties.hours")}: {p.opening_hours}</p>}
          {p.instructions && <p className="text-xs text-ink-tertiary">{t("shipments.parties.instructions")}: {p.instructions}</p>}
        </div>
      ) : <p className="mt-0.5 text-sm text-ink-tertiary">—</p>}
    </div>
  );
}

export function PartiesFacts({ s }) {
  const { t } = useT();
  const p = s.parties || {};
  return (
    <Section title={t("shipments.section.parties")}>
      <div className="flex flex-col gap-3">
        <PartyBlock label={t("shipments.parties.shipper")} p={p.shipper} />
        <PartyBlock label={t("shipments.parties.consignee")} p={p.consignee} />
        {p.notify && <PartyBlock label={t("shipments.parties.notify")} p={p.notify} />}
      </div>
    </Section>
  );
}

export function ReferencesFacts({ s, canEdit, onSaved }) {
  const { t } = useT();
  const refs = s.references || {};
  const [editing, setEditing] = useState(false);
  const [ref, setRef] = useState(refs.customer_ref || "");
  const [pos, setPos] = useState((refs.po_numbers || []).join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(false);
  const docs = (s.legs || []).map((l) => l.transport_doc_no).filter(Boolean);
  const conts = [...new Set((s.legs || []).flatMap((l) => l.container_numbers || []))];

  const save = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await apiPatch(`/shipments/${s.id}/references`, {
        customer_ref: ref, po_numbers: pos.split(",").map((x) => x.trim()).filter(Boolean),
      });
      setEditing(false);
      setSaved(true);
      onSaved?.(r.references);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title={t("shipments.section.references")}
      actions={canEdit && !editing ? (
        <span className="inline-flex items-center gap-2">
          {saved && <span className="inline-flex items-center gap-1 text-xs text-success-fg" role="status"><Check className="size-3.5" />{t("shipments.form.saved")}</span>}
          <Button size="xs" variant="ghost" onClick={() => { setEditing(true); setSaved(false); }}><Pencil /> {t("action.edit")}</Button>
        </span>
      ) : null}>
      {editing ? (
        <form onSubmit={save} className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="ref-customer">{t("shipments.refs.customer_ref")}</Label>
            <Input id="ref-customer" value={ref} maxLength={80} onChange={(e) => setRef(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="ref-po">{t("shipments.refs.po_numbers")}</Label>
            <Input id="ref-po" value={pos} onChange={(e) => setPos(e.target.value)} />
            <p className="text-xs text-ink-tertiary">{t("shipments.refs.po_hint")}</p>
          </div>
          {error && <ErrorState error={error} compact />}
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="outline" onClick={() => { setEditing(false); setError(null); }}>{t("action.cancel")}</Button>
            <Button size="sm" type="submit" disabled={busy}>{busy ? t("state.saving") : t("action.save")}</Button>
          </div>
        </form>
      ) : (
        <KV cols={1} items={[
          { label: t("shipments.refs.customer_ref"), value: refs.customer_ref, mono: true },
          { label: t("shipments.refs.po_numbers"), value: (refs.po_numbers || []).join(", "), mono: true },
          refs.booking_no ? { label: t("shipments.refs.booking_no"), value: refs.booking_no, mono: true } : null,
          { label: t("shipments.refs.transport_docs"), value: [...new Set([...(refs.transport_doc_nos || []), ...docs])].join(", "), mono: true },
          conts.length ? { label: t("shipments.refs.containers"), value: conts.join(", "), mono: true } : null,
        ]} />
      )}
    </Section>
  );
}

export function CompanyChecks({ s }) {
  const { t } = useT();
  const c = s.company;
  if (!c) return null;
  return (
    <Section title={t("shipments.section.company")}>
      <KV cols={1} items={[
        { label: t("shipments.detail.customer"), value: c.name },
        c.business_id ? { label: t("shipments.company.business_id"), value: c.business_id, mono: true } : null,
        { label: t("shipments.company.kyc"), value: c.kyc_status ? <StatusBadge group="kyc" value={c.kyc_status} size="sm" /> : null },
        { label: t("shipments.company.sanctions"), value: c.sanctions_status ? <StatusBadge group="generic" value={c.sanctions_status === "clear" ? "ok" : "warning"} label={tr(t, `shipments.sanctions.${c.sanctions_status}`, c.sanctions_status)} size="sm" /> : null },
      ]} />
    </Section>
  );
}

export function TrackingFeeds({ s }) {
  const { t } = useT();
  const feeds = s.tracking_adapters || [];
  if (!feeds.length) return null;
  return (
    <Section title={t("shipments.section.feeds")}>
      <ul className="flex flex-col gap-1 text-sm">
        {feeds.map((f) => (
          <li key={f.seq} className="flex gap-2">
            <span className="w-14 shrink-0 text-xs text-ink-tertiary">{t("shipments.leg.seq")} {f.seq}</span>
            <span className={f.adapter && !f.configured ? "text-warning-fg" : "text-ink-secondary"}>
              {!f.adapter ? t("shipments.feeds.none") : f.configured ? t("shipments.feeds.configured", { name: f.name }) : t("shipments.feeds.not_configured", { name: f.name })}
            </span>
          </li>
        ))}
      </ul>
    </Section>
  );
}
