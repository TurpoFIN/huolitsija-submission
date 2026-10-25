// Portal: book a quoted option. One page, three blocks (route & cargo, parties, pickup & delivery + references)
// and a sticky price card that names the amount on the confirm button. Draft is kept per quote in localStorage.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowRight, Loader2, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { DateInput } from "@/components/ui/DateInput";
import { TimeInput } from "@/components/ui/TimeInput";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { PageHeader, Section } from "@/components/app/layout";
import { EmptyState, ErrorState } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, dateShort, num } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import CargoFields from "@/features/quotes/CargoFields";
import RouteText from "@/features/quotes/RouteText";
import WarningsList from "@/features/quotes/WarningsList";
import BookingBlockers from "@/features/quotes/BookingBlockers";
import { Field, NumberInput } from "@/features/quotes/fields";
import { eoriErrorParty, eoriShapeOk } from "@/lib/eori";
import { countryName, emptyCargo, eur, isExpired, legsLabel, lineText, modeIcon, otherPartyInfo, validityLabel } from "@/features/quotes/quoteFormat";

const DRAFT_KEY = (quoteId) => `huo.booking.draft.${quoteId}`;
const PALLET_DIMS = { pallet_eur: [120, 80], pallet_fin: [120, 100] };
const NO_PG_CLASSES = ["1", "2.1", "2.2", "2.3", "7"];
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const todayIso = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

const emptyAddress = (country = "") => ({ street: "", postal_code: "", city: "", country });
const emptyContact = () => ({ name: "", email: "", phone: "" });
const emptyParty = (country = "") => ({ name: "", address: emptyAddress(country), contact: emptyContact(), eori: "" });

function companyParty(company, user) {
  if (!company) return null;
  const a = company.address || {};
  return {
    name: company.name || "",
    address: { street: a.street || "", postal_code: a.postal_code || "", city: a.city || "", country: a.country || company.country || "" },
    contact: { name: user?.name || "", email: user?.email || company.billing_email || "", phone: user?.phone || company.phone || "" },
    eori: company.eori || "",
  };
}

/** Which side is the customer? seller -> shipper, buyer -> consignee. */
function customerSide(req) {
  if (req.customer_role === "seller") return "shipper";
  if (req.customer_role === "buyer") return "consignee";
  if (req.direction === "export" || req.direction === "domestic") return "shipper";
  if (req.direction === "import") return "consignee";
  return null;
}

function initialForm(quote, option, company, user) {
  const req = quote.request || {};
  const cargo = { ...emptyCargo(), ...(req.cargo || {}) };
  const dims = PALLET_DIMS[cargo.package_type] && cargo.load_type !== "FCL"
    ? [{ count: cargo.packages ?? null, length_cm: PALLET_DIMS[cargo.package_type][0], width_cm: PALLET_DIMS[cargo.package_type][1], height_cm: null }]
    : [{ count: cargo.packages ?? null, length_cm: null, width_cm: null, height_cm: null }];
  if (cargo.dangerous_goods) cargo.dg = { un_number: "", class: null, packing_group: null, proper_shipping_name: "", emergency_phone: "", ...(cargo.dg || {}) };
  const side = customerSide(req);
  const mine = companyParty(company, user);
  const shipper = side === "shipper" && mine ? mine : emptyParty(req.origin?.country || "");
  const consignee = side === "consignee" && mine ? mine : emptyParty(req.destination?.country || "");
  return {
    cargo: { ...cargo, dims },
    incoterm_place: req.incoterm_place || "",
    shipper, consignee, notifyOn: false, notify: emptyParty(),
    pickup: { same: true, address: emptyAddress(req.origin?.country || ""), date: req.ready_date && req.ready_date >= todayIso() ? req.ready_date : "", from: "", to: "", contact: emptyContact(), instructions: "" },
    delivery: { same: true, address: emptyAddress(req.destination?.country || ""), date: "", from: "", to: "", contact: emptyContact(), instructions: "" },
    customer_reference: "", po_numbers: [], special_instructions: "", accept_terms: false,
  };
}

function setPath(obj, path, value) {
  const keys = path.split(".");
  const out = Array.isArray(obj) ? [...obj] : { ...obj };
  let cur = out;
  keys.forEach((k, i) => {
    if (i === keys.length - 1) cur[k] = value;
    else {
      cur[k] = Array.isArray(cur[k]) ? [...cur[k]] : { ...(cur[k] || {}) };
      cur = cur[k];
    }
  });
  return out;
}

function validate(f, ctx, t) {
  const e = {};
  const req = (path, v, msg) => {
    if (v === null || v === undefined || String(v).trim() === "") e[path] = msg || t("pq.b.err.required");
  };
  const party = (p, key) => {
    req(`${key}.name`, f[key].name);
    req(`${key}.address.street`, f[key].address.street);
    req(`${key}.address.postal_code`, f[key].address.postal_code);
    req(`${key}.address.city`, f[key].address.city);
    if (!/^[A-Za-z]{2}$/.test(f[key].address.country || "")) e[`${key}.address.country`] = t("pq.b.err.country");
    if (f[key].contact.email && !EMAIL_RE.test(f[key].contact.email)) e[`${key}.contact.email`] = t("pq.b.err.email");
    if (f[key].eori?.trim() && !eoriShapeOk(f[key].eori)) e[`${key}.eori`] = t("pq.b.err.eori");
  };
  party(f.shipper, "shipper");
  party(f.consignee, "consignee");
  if (f.notifyOn) {
    req("notify.name", f.notify.name);
    if (f.notify.contact.email && !EMAIL_RE.test(f.notify.contact.email)) e["notify.contact.email"] = t("pq.b.err.email");
    if (f.notify.eori?.trim() && !eoriShapeOk(f.notify.eori)) e["notify.eori"] = t("pq.b.err.eori");
  }
  if (ctx.hasPickup) {
    if (!f.pickup.same) {
      req("pickup.address.street", f.pickup.address.street);
      req("pickup.address.postal_code", f.pickup.address.postal_code);
      req("pickup.address.city", f.pickup.address.city);
      if (!/^[A-Za-z]{2}$/.test(f.pickup.address.country || "")) e["pickup.address.country"] = t("pq.b.err.country");
    }
    if (!f.pickup.date) e["pickup.date"] = t("pq.b.err.pickup_date");
  }
  if (f.pickup.date && f.pickup.date < todayIso()) e["pickup.date"] = t("pq.b.err.date_past");
  if (ctx.hasDelivery && !f.delivery.same) {
    req("delivery.address.street", f.delivery.address.street);
    req("delivery.address.postal_code", f.delivery.address.postal_code);
    req("delivery.address.city", f.delivery.address.city);
    if (!/^[A-Za-z]{2}$/.test(f.delivery.address.country || "")) e["delivery.address.country"] = t("pq.b.err.country");
  }
  const tw = (k) => {
    const s = f[k];
    if ((s.from && !s.to) || (!s.from && s.to)) e[`${k}.window`] = t("pq.b.err.window");
    else if (s.from && s.to && s.from >= s.to) e[`${k}.window`] = t("pq.b.err.window_order");
  };
  tw("pickup");
  tw("delivery");
  const c = f.cargo;
  if (!c.description || !c.description.trim()) e["cargo.description"] = t("pq.b.err.description");
  if (c.load_type === "FCL") {
    if (!(c.containers || []).some((x) => x.type && x.count >= 1)) e["cargo.containers"] = t("pq.err.containers");
  } else {
    if (!c.packages || c.packages < 1) e["cargo.packages"] = t("pq.err.packages");
    if (!c.weight_kg || c.weight_kg <= 0) e["cargo.weight_kg"] = t("pq.err.weight");
  }
  if (c.dangerous_goods) {
    if (!/^UN\d{4}$/.test((c.dg?.un_number || "").trim())) e["cargo.dg.un_number"] = t("pq.err.un_number");
    req("cargo.dg.proper_shipping_name", c.dg?.proper_shipping_name);
    if (!c.dg?.class) e["cargo.dg.class"] = t("pq.err.dg_class");
    if (c.dg?.class && !NO_PG_CLASSES.includes(c.dg.class) && !c.dg?.packing_group) e["cargo.dg.packing_group"] = t("pq.b.err.pg");
    req("cargo.dg.emergency_phone", c.dg?.emergency_phone);
  }
  (c.dims || []).forEach((d, i) => {
    const any = d.count || d.length_cm || d.width_cm || d.height_cm;
    if (any && !(d.count && d.length_cm && d.width_cm && d.height_cm)) e[`cargo.dims.${i}`] = t("pq.b.err.dims");
  });
  if (ctx.incoterm) req("incoterm_place", f.incoterm_place, t("pq.err.place"));
  if (!f.accept_terms) e.accept_terms = t("pq.b.err.terms");
  return e;
}

function buildBody(f, quote, option) {
  const party = (p) => ({
    name: p.name.trim(),
    address: { ...p.address, country: (p.address.country || "").toUpperCase() },
    contact: { name: p.contact.name.trim() || null, email: p.contact.email.trim() || null, phone: p.contact.phone.trim() || null },
    eori: p.eori?.trim() || null,
  });
  const contactOr = (c, fallback) => (c.name || c.email || c.phone
    ? { name: c.name.trim() || null, phone: c.phone.trim() || null, email: c.email.trim() || null }
    : { name: fallback.contact.name || null, phone: fallback.contact.phone || null, email: fallback.contact.email || null });
  const stop = (s, partyObj) => ({
    address: s.same ? { ...partyObj.address, country: (partyObj.address.country || "").toUpperCase() } : { ...s.address, country: (s.address.country || "").toUpperCase() },
    date: s.date || null,
    window: s.from && s.to ? `${s.from}-${s.to}` : null,
    contact: contactOr(s.contact, partyObj),
    instructions: s.instructions.trim() || null,
  });
  const c = { ...f.cargo };
  c.dims = (c.dims || []).filter((d) => d.count && d.length_cm && d.width_cm && d.height_cm);
  if (c.load_type !== "FCL") c.containers = [];
  if (!c.dangerous_goods) c.dg = null;
  return {
    quote_id: quote.id,
    option_id: option.id,
    shipper: party(f.shipper),
    consignee: party(f.consignee),
    notify: f.notifyOn ? party(f.notify) : null,
    pickup: stop(f.pickup, f.shipper),
    delivery: stop(f.delivery, f.consignee),
    cargo: c,
    incoterm_place: f.incoterm_place.trim() || null,
    customer_reference: f.customer_reference.trim() || null,
    po_numbers: f.po_numbers,
    special_instructions: f.special_instructions.trim() || null,
    accept_terms: !!f.accept_terms,
  };
}

/* ---------- small field groups ---------- */

function AddressFields({ prefix, value, onChange, errors, locale, t, disabled }) {
  const id = (k) => `${prefix.replace(/\./g, "-")}-${k}`;
  const err = (k) => errors[`${prefix}.${k}`];
  return (
    <div className="grid grid-cols-2 gap-x-3 gap-y-3 md:grid-cols-[minmax(0,2fr)_7rem_minmax(0,1fr)_5.5rem]">
      <Field label={t("pq.b.f.street")} htmlFor={id("street")} error={err("street")} className="col-span-2 md:col-span-1">
        <Input id={id("street")} autoComplete="street-address" value={value.street} disabled={disabled} aria-invalid={err("street") ? "true" : undefined}
          onChange={(e) => onChange({ ...value, street: e.target.value })} />
      </Field>
      <Field label={t("pq.b.f.postal_code")} htmlFor={id("postal_code")} error={err("postal_code")}>
        <Input id={id("postal_code")} autoComplete="postal-code" value={value.postal_code} disabled={disabled} aria-invalid={err("postal_code") ? "true" : undefined}
          onChange={(e) => onChange({ ...value, postal_code: e.target.value })} />
      </Field>
      <Field label={t("pq.b.f.city")} htmlFor={id("city")} error={err("city")}>
        <Input id={id("city")} autoComplete="address-level2" value={value.city} disabled={disabled} aria-invalid={err("city") ? "true" : undefined}
          onChange={(e) => onChange({ ...value, city: e.target.value })} />
      </Field>
      <Field label={t("pq.b.f.country")} htmlFor={id("country")} error={err("country")} hint={value.country?.length === 2 ? countryName(value.country, locale) : null}>
        <Input id={id("country")} autoComplete="country" value={value.country} maxLength={2} placeholder="FI" disabled={disabled}
          className="font-mono uppercase" aria-invalid={err("country") ? "true" : undefined}
          onChange={(e) => onChange({ ...value, country: e.target.value.toUpperCase() })} />
      </Field>
    </div>
  );
}

function ContactFields({ prefix, value, onChange, errors, t, placeholders }) {
  const id = (k) => `${prefix.replace(/\./g, "-")}-${k}`;
  const err = (k) => errors[`${prefix}.${k}`];
  return (
    <div className="grid gap-x-3 gap-y-3 md:grid-cols-3">
      <Field label={t("pq.b.f.contact_name")} htmlFor={id("name")} error={err("name")}>
        <Input id={id("name")} autoComplete="name" value={value.name} placeholder={placeholders?.name} onChange={(e) => onChange({ ...value, name: e.target.value })} />
      </Field>
      <Field label={t("field.email")} htmlFor={id("email")} error={err("email")}>
        <Input id={id("email")} type="email" autoComplete="email" value={value.email} placeholder={placeholders?.email}
          aria-invalid={err("email") ? "true" : undefined} onChange={(e) => onChange({ ...value, email: e.target.value })} />
      </Field>
      <Field label={t("pq.b.f.phone")} htmlFor={id("phone")} error={err("phone")}>
        <Input id={id("phone")} type="tel" autoComplete="tel" value={value.phone} placeholder={placeholders?.phone || "+358 …"}
          onChange={(e) => onChange({ ...value, phone: e.target.value })} />
      </Field>
    </div>
  );
}

function PartyBlock({ k, title, hint, value, onChange, errors, locale, t, mine }) {
  return (
    <fieldset className="flex flex-col gap-3 border-t border-[color:var(--border-subtle)] pt-3 first:border-0 first:pt-0">
      <legend className="sr-only">{title}</legend>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">{title}</h3>
        {hint && <span className="text-xs text-ink-tertiary">{hint}</span>}
      </div>
      <div className="grid gap-x-3 gap-y-3 md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Field label={t("pq.b.f.company_name")} htmlFor={`${k}-name`} error={errors[`${k}.name`]}>
          <Input id={`${k}-name`} autoComplete="organization" value={value.name} aria-invalid={errors[`${k}.name`] ? "true" : undefined}
            onChange={(e) => onChange({ ...value, name: e.target.value })} />
        </Field>
        <Field label={t("pq.b.f.eori")} htmlFor={`${k}-eori`} hint={mine ? null : t("pq.b.f.eori_hint")} error={errors[`${k}.eori`]}>
          <Input id={`${k}-eori`} value={value.eori} className="font-mono uppercase" placeholder="FI1234567-8"
            aria-invalid={errors[`${k}.eori`] ? "true" : undefined}
            onChange={(e) => onChange({ ...value, eori: e.target.value.toUpperCase() })} />
        </Field>
      </div>
      <AddressFields prefix={`${k}.address`} value={value.address} onChange={(a) => onChange({ ...value, address: a })} errors={errors} locale={locale} t={t} />
      <ContactFields prefix={`${k}.contact`} value={value.contact} onChange={(c) => onChange({ ...value, contact: c })} errors={errors} t={t} />
    </fieldset>
  );
}

function StopBlock({ k, title, value, onChange, partyLabel, party, errors, locale, t, required, sameLabel, handover }) {
  return (
    <fieldset className="flex flex-col gap-3 border-t border-[color:var(--border-subtle)] pt-3 first:border-0 first:pt-0">
      <legend className="sr-only">{title}</legend>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">{title}</h3>
        {!required && handover && <span className="text-xs text-ink-tertiary">{handover}</span>}
      </div>
      <label className="flex items-center gap-2 text-sm">
        <Checkbox checked={value.same} onCheckedChange={(v) => onChange({ ...value, same: !!v })} />
        {sameLabel}
        {value.same && party?.address?.city && <span className="text-ink-tertiary">· {[party.address.street, party.address.city].filter(Boolean).join(", ")}</span>}
      </label>
      {!value.same && (
        <AddressFields prefix={`${k}.address`} value={value.address} onChange={(a) => onChange({ ...value, address: a })} errors={errors} locale={locale} t={t} />
      )}
      <div className="grid grid-cols-2 gap-x-3 gap-y-3 md:grid-cols-[10rem_7rem_7rem_minmax(0,1fr)]">
        <Field label={required && k === "pickup" ? t("pq.b.f.date_req") : t("pq.b.f.date_opt")} htmlFor={`${k}-date`} error={errors[`${k}.date`]} className="col-span-2 md:col-span-1">
          <DateInput id={`${k}-date`} min={todayIso()} value={value.date || ""} aria-invalid={errors[`${k}.date`] ? "true" : undefined}
            clearable onChange={(iso) => onChange({ ...value, date: iso })} />
        </Field>
        <Field label={t("pq.b.f.from")} htmlFor={`${k}-window`} error={errors[`${k}.window`]}>
          <TimeInput id={`${k}-window`} value={value.from} onChange={(hhmm) => onChange({ ...value, from: hhmm })} aria-invalid={errors[`${k}.window`] ? "true" : undefined} />
        </Field>
        <Field label={t("pq.b.f.to")} htmlFor={`${k}-to`}>
          <TimeInput id={`${k}-to`} value={value.to} onChange={(hhmm) => onChange({ ...value, to: hhmm })} aria-invalid={errors[`${k}.window`] ? "true" : undefined} />
        </Field>
      </div>
      <ContactFields prefix={`${k}.contact`} value={value.contact} onChange={(c) => onChange({ ...value, contact: c })} errors={errors} t={t}
        placeholders={{ name: party?.contact?.name, email: party?.contact?.email, phone: party?.contact?.phone }} />
      <p className="-mt-1 text-xs text-ink-tertiary">{t("pq.b.f.contact_default", { party: partyLabel })}</p>
      <Field label={t("pq.b.f.instructions")} htmlFor={`${k}-instructions`}>
        <Input id={`${k}-instructions`} value={value.instructions} maxLength={300} placeholder={t(`pq.b.ph.${k}_instructions`)}
          onChange={(e) => onChange({ ...value, instructions: e.target.value })} />
      </Field>
    </fieldset>
  );
}

function DimsRows({ dims, onChange, errors, t, locale, volume }) {
  const rows = dims && dims.length ? dims : [];
  const setRow = (i, patch) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const total = rows.reduce((a, r) => (r.count && r.length_cm && r.width_cm && r.height_cm ? a + (r.count * r.length_cm * r.width_cm * r.height_cm) / 1e6 : a), 0);
  const diff = volume && total ? Math.abs(total - volume) / volume : 0;
  return (
    <div className="flex flex-col gap-2" role="group" aria-label={t("pq.b.dims.title")}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-ink-secondary">{t("pq.b.dims.title")}</span>
        {total > 0 && (
          <span className={cn("text-xs tabular", diff > 0.1 ? "text-warning-fg" : "text-ink-tertiary")}>
            {t("pq.b.dims.total", { v: num(total, locale, 2) })}{diff > 0.1 ? ` · ${t("pq.b.dims.differs", { v: num(volume, locale, 2) })}` : ""}
          </span>
        )}
      </div>
      {rows.map((r, i) => (
        <div key={i} className="flex flex-col gap-1">
          <div className="grid grid-cols-[3.25rem_auto_repeat(3,minmax(0,1fr))_auto_auto] items-center gap-1.5 sm:flex sm:gap-2">
            <NumberInput integer min={1} value={r.count ?? null} onChange={(v) => setRow(i, { count: v })} className="sm:w-16" aria-label={t("pq.b.dims.count")} placeholder={t("pq.b.dims.count_ph")} />
            <span className="text-sm text-ink-tertiary" aria-hidden>×</span>
            <NumberInput value={r.length_cm ?? null} onChange={(v) => setRow(i, { length_cm: v })} className="sm:w-20" suffix="L" aria-label={t("pq.b.dims.length")} />
            <NumberInput value={r.width_cm ?? null} onChange={(v) => setRow(i, { width_cm: v })} className="sm:w-20" suffix="W" aria-label={t("pq.b.dims.width")} />
            <NumberInput value={r.height_cm ?? null} onChange={(v) => setRow(i, { height_cm: v })} className="sm:w-20" suffix="H" aria-label={t("pq.b.dims.height")} />
            <span className="text-xs text-ink-tertiary">cm</span>
            <Button variant="ghost" size="icon-sm" aria-label={t("action.delete")} onClick={() => onChange(rows.filter((_, j) => j !== i))}><Trash2 /></Button>
          </div>
          {errors[`cargo.dims.${i}`] && <p role="alert" className="text-xs text-danger-fg">{errors[`cargo.dims.${i}`]}</p>}
        </div>
      ))}
      <div>
        <Button variant="ghost" size="sm" onClick={() => onChange([...rows, { count: null, length_cm: null, width_cm: null, height_cm: null }])}>
          <Plus /> {t("pq.b.dims.add")}
        </Button>
      </div>
    </div>
  );
}

function PoChips({ value, onChange, t }) {
  const [text, setText] = useState("");
  const add = () => {
    const parts = text.split(/[,;\n]/).map((s) => s.trim()).filter(Boolean);
    if (!parts.length) return;
    onChange(Array.from(new Set([...value, ...parts])).slice(0, 30));
    setText("");
  };
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {value.map((po) => (
          <span key={po} className="inline-flex h-6 items-center gap-1 rounded-full bg-secondary pl-2.5 pr-1 font-mono text-xs">
            {po}
            <button type="button" onClick={() => onChange(value.filter((x) => x !== po))} aria-label={t("pq.b.po.remove", { po })}
              className="rounded-full p-0.5 hover:bg-hover"><X className="size-3" /></button>
          </span>
        ))}
        <Input id="b-po" value={text} placeholder={value.length ? t("pq.b.po.more") : "PO-12345"} className="h-7 w-40"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add();
            } else if (e.key === "Backspace" && !text && value.length) onChange(value.slice(0, -1));
          }}
          onBlur={add} />
      </div>
    </div>
  );
}

/* ---------- price card ---------- */

function PriceCard({ quote, option, form, setField, errors, submitting, onSubmit, savedAt, blockedReason, submitError, onRetry, t, locale }) {
  const req = quote.request || {};
  const valid = option.valid_until || quote.valid_until;
  const notIncluded = option.not_included || [];
  return (
    <div className="flex flex-col gap-3 rounded-xl bg-surface px-4 py-3 shadow-[inset_0_0_0_0.5px_var(--border-default)]">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium">{option.carrier_name}</p>
        <p className="truncate text-xs text-ink-tertiary">{option.service_name || option.service}</p>
      </div>
      <dl className="flex flex-col gap-1 text-sm">
        <div className="flex items-baseline justify-between gap-3">
          <dt>{t("quotes.bd.total_excl")}</dt>
          <dd className="text-lg tabular whitespace-nowrap">{eur(option.total_cents, locale)}</dd>
        </div>
        <div className="flex items-baseline justify-between gap-3 text-xs text-ink-tertiary">
          <dt>{req.incoterm ? `${req.incoterm} ${form.incoterm_place || req.incoterm_place || ""}`.trim() : t("pq.card.no_incoterm")}</dt>
        </div>
        <div className="flex items-baseline justify-between gap-3 text-ink-secondary">
          <dt>{t("quotes.bd.vat_est")}</dt>
          <dd className="tabular whitespace-nowrap">{eur(option.vat_cents, locale)}</dd>
        </div>
        <div className="flex items-baseline justify-between gap-3 text-ink-secondary">
          <dt>{t("quotes.bd.total_incl")}</dt>
          <dd className="tabular whitespace-nowrap">{eur(option.total_incl_vat_cents, locale)}</dd>
        </div>
      </dl>
      {otherPartyInfo(option).partial && (
        <p className="text-xs text-warning-fg">{t("quotes.share.note", { incoterm: req.incoterm || "" })}</p>
      )}
      <div className="text-xs">
        <p className="text-ink-secondary">{t("quotes.bd.not_included")}</p>
        {notIncluded.length === 0 ? (
          <p className="text-ink-tertiary">{t("quotes.bd.not_included_none")}</p>
        ) : (
          <ul className="mt-0.5 flex flex-col gap-0.5 text-ink-tertiary">
            {notIncluded.map((n, i) => (
              <li key={i} className="flex justify-between gap-3">
                <span className="min-w-0">{lineText(n, locale)}</span>
                {n.amount_cents !== null && n.amount_cents !== undefined && <span className="tabular whitespace-nowrap">≈ {eur(n.amount_cents, locale)}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
      <p className={cn("text-xs", isExpired(valid) ? "text-danger-fg" : "text-ink-secondary")}>
        {t("pq.card.rate_expires", { date: date(valid, locale) })}
        <span className="text-ink-tertiary"> · {validityLabel(t, valid)}</span>
      </p>
      <div className="flex flex-col gap-1">
        <label className="flex items-start gap-2 text-sm">
          <Checkbox className="mt-0.5" checked={form.accept_terms} onCheckedChange={(v) => setField("accept_terms", !!v)}
            aria-invalid={errors.accept_terms ? "true" : undefined} aria-describedby={errors.accept_terms ? "b-terms-err" : undefined} id="b-terms" />
          <span>
            {t("pq.b.terms.before")}{" "}
            <Link to="/terms" target="_blank" className="underline underline-offset-2">{quote.terms_version || "NSAB 2015 (PSYM 2015)"}</Link>{" "}
            {t("pq.b.terms.after")}
          </span>
        </label>
        {errors.accept_terms && <p id="b-terms-err" role="alert" className="ml-6 text-xs text-danger-fg">{errors.accept_terms}</p>}
      </div>
      {submitError && (
        <div role="alert" className="flex flex-col gap-2 rounded-lg px-3 py-2 text-sm status-failed">
          <span>{submitError.message}</span>
          {submitError.detail && <span className="text-xs opacity-80">{submitError.detail}</span>}
          {submitError.kyc && (
            <Link to="/portal/company" className="inline-flex items-center gap-1 font-medium underline underline-offset-2">
              {t("pq.b.err.open_company")} <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          )}
          {(submitError.status === 503 || submitError.status === 0) && (
            <div><Button size="xs" variant="outline" onClick={onRetry}><RefreshCw /> {t("action.retry")}</Button></div>
          )}
        </div>
      )}
      <Button size="lg" onClick={onSubmit} disabled={submitting || !!blockedReason}>
        {submitting && <Loader2 className="animate-spin" />}
        {submitting ? t("pq.b.submitting") : t("pq.b.confirm", { amount: eur(option.total_cents, locale) })}
      </Button>
      {blockedReason && <p className="text-xs text-ink-secondary">{blockedReason}</p>}
      <p className="text-xs text-ink-tertiary" aria-live="polite">
        {savedAt ? t("pq.b.draft_saved", { time: savedAt.toLocaleTimeString(locale === "fi" ? "fi-FI" : "en-GB", { hour: "2-digit", minute: "2-digit" }) }) : t("pq.b.draft_hint")}
      </p>
    </div>
  );
}

/* ---------- page ---------- */

function BookingForm({ quote, option }) {
  const { t, locale } = useT();
  const { company, user } = useAuth();
  const navigate = useNavigate();
  const req = quote.request || {};
  const [form, setForm] = useState(() => initialForm(quote, option, company, user));
  const [restored, setRestored] = useState(null);
  const [savedAt, setSavedAt] = useState(null);
  const [errors, setErrors] = useState({});
  const [tried, setTried] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  const dirty = useRef(false);
  const draftKey = DRAFT_KEY(quote.id);

  const lines = option.lines || [];
  const legs = option.legs || [];
  const hasPickup = lines.some((l) => l.stage === "origin") || legs.some((l) => l.kind === "pre_carriage");
  const hasDelivery = lines.some((l) => l.stage === "destination") || legs.some((l) => l.kind === "on_carriage");
  const ctx = { hasPickup, hasDelivery, incoterm: req.incoterm };
  const side = customerSide(req);

  // restore draft once
  useEffect(() => {
    try {
      const raw = localStorage.getItem(draftKey);
      if (!raw) return;
      const d = JSON.parse(raw);
      if (d && d.form && d.option_id === option.id) {
        setForm((f) => ({ ...f, ...d.form, accept_terms: false }));
        setRestored(new Date(d.saved_at));
      }
    } catch (e) {}
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey, option.id]);

  // company may arrive after first render: fill the customer's side if it is still empty
  useEffect(() => {
    if (!company || !side || dirty.current) return;
    const mine = companyParty(company, user);
    setForm((f) => (f[side].name ? f : { ...f, [side]: mine }));
  }, [company, user, side]);

  // autosave draft
  useEffect(() => {
    if (!dirty.current) return undefined;
    const h = setTimeout(() => {
      try {
        const { accept_terms, ...rest } = form;
        const now = new Date();
        localStorage.setItem(draftKey, JSON.stringify({ option_id: option.id, form: rest, saved_at: now.toISOString() }));
        setSavedAt(now);
      } catch (e) {}
    }, 700);
    return () => clearTimeout(h);
  }, [form, draftKey, option.id]);

  const change = useCallback((updater) => {
    dirty.current = true;
    setForm((f) => {
      const next = typeof updater === "function" ? updater(f) : updater;
      if (tried) setErrors(validate(next, ctx, t));
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tried, t, hasPickup, hasDelivery]);
  const setField = (path, v) => change((f) => setPath(f, path, v));

  const discardDraft = () => {
    try {
      localStorage.removeItem(draftKey);
    } catch (e) {}
    dirty.current = false;
    setForm(initialForm(quote, option, company, user));
    setRestored(null);
    setSavedAt(null);
  };

  const expired = quote.status === "expired" || isExpired(option.valid_until || quote.valid_until);
  const blockers = quote.booking_blockers || [];
  let blockedReason = null;
  if (quote.status === "booked") blockedReason = t("pq.reason.booked");
  else if (expired) blockedReason = t("pq.b.blocked.expired");
  else if (quote.status === "pending_approval") blockedReason = t("pq.reason.review");
  else if (quote.status === "rejected") blockedReason = t("pq.reason.rejected");

  const submit = async () => {
    setTried(true);
    const errs = validate(form, ctx, t);
    setErrors(errs);
    setSubmitError(null);
    const keys = Object.keys(errs);
    if (keys.length) {
      const k = keys[0];
      const el = document.getElementById(k === "accept_terms" ? "b-terms" : k.replace(/\./g, "-")) || document.getElementById(k);
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        el.focus({ preventScroll: true });
      }
      return;
    }
    setSubmitting(true);
    try {
      const r = await apiPost("/bookings", buildBody(form, quote, option));
      try {
        localStorage.removeItem(draftKey);
      } catch (e) {}
      if (r.prepayment) {
        toast.success(t("pq.b.prepay", { number: r.prepayment.invoice_number || "" }));
        navigate(`/portal/invoices/${r.prepayment.invoice_id}`);
        return;
      }
      toast.success(t("pq.b.done", { ref: r.shipment_reference || "" }));
      if (r.shipment_id) navigate(`/portal/shipments/${r.shipment_id}`);
      else navigate(`/portal/quotes/${quote.id}`);
    } catch (e) {
      const eoriParty = e.status === 400 ? eoriErrorParty(e.data?.detail) : null;
      if (eoriParty) {
        setErrors((prev) => ({ ...prev, [`${eoriParty}.eori`]: t("pq.b.err.eori") }));
        document.getElementById(`${eoriParty}-eori`)?.focus();
        setSubmitError({ status: e.status, message: t("pq.b.err.eori_party", { party: t(`pq.b.party.${eoriParty}`) }) });
        setSubmitting(false);
        return;
      }
      const kyc = e.status === 409 && /\/portal\/company/.test(e.message || "");
      setSubmitError({
        status: e.status, kyc,
        message: e.status === 0 ? t("state.offline") : e.status === 503 ? t("pq.b.err.unavailable") : kyc ? t("pq.b.err.kyc") : e.message,
        detail: kyc ? e.message : null,
      });
      setSubmitting(false);
    }
  };

  const cargoErrors = useMemo(() => {
    const out = {};
    Object.entries(errors).forEach(([k, v]) => {
      if (k.startsWith("cargo.") && !k.startsWith("cargo.dims.")) out[k.slice(6)] = v;
    });
    return out;
  }, [errors]);

  const ModeIcon = modeIcon(option.mode);
  const errCount = Object.keys(errors).length;
  const partyLabel = { shipper: t("pq.b.party.shipper"), consignee: t("pq.b.party.consignee") };

  const card = (
    <PriceCard quote={quote} option={option} form={form} setField={setField} errors={errors} submitting={submitting}
      onSubmit={submit} savedAt={savedAt} blockedReason={blockedReason} submitError={submitError} onRetry={submit} t={t} locale={locale} />
  );

  return (
    <>
      <PageHeader
        crumbs={[{ label: t("nav.quotes"), to: "/portal/quotes" }, { label: quote.number || "—", to: `/portal/quotes/${quote.id}` }, { label: t("pq.b.crumb") }]}
        title={t("pq.b.title")}
        meta={
          <>
            <RouteText origin={req.origin} destination={req.destination} className="text-foreground" />
            <span className="text-ink-tertiary">·</span>
            <span>{option.carrier_name}</span>
            <span className="text-ink-tertiary">·</span>
            <span className="tabular">{t("pq.valid_until", { date: date(option.valid_until || quote.valid_until, locale) })}</span>
          </>
        }
      />
      {restored && (
        <p className="mb-3 flex flex-wrap items-center gap-2 text-sm text-ink-secondary" role="status">
          {t("pq.b.draft_restored", { time: restored.toLocaleString(locale === "fi" ? "fi-FI" : "en-GB", { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" }) })}
          <Button size="xs" variant="ghost" onClick={discardDraft}>{t("pq.b.draft_discard")}</Button>
        </p>
      )}
      <BookingBlockers className="mb-4" items={blockers} company={quote.company} />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_21rem]">
        <div className="min-w-0">
          <Section title={t("pq.b.sec.route")}>
            <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
              <span className="inline-flex items-center gap-1.5"><ModeIcon className="size-4 text-ink-tertiary" aria-hidden />{tEnum(t, "mode", option.mode)}</span>
              <span className="font-mono text-xs text-ink-secondary">{legsLabel(legs) || "—"}</span>
              <span className="tabular text-ink-secondary">{dateShort(option.etd, locale)} → {dateShort(option.eta, locale)}</span>
              <span className="tabular text-ink-secondary">{option.transit_days !== null && option.transit_days !== undefined ? t("quotes.days", { n: option.transit_days }) : "—"}</span>
            </div>
            {req.incoterm && (
              <div className="mb-4 grid gap-3 md:grid-cols-[8rem_minmax(0,1fr)]">
                <Field label={t("field.incoterm")}><div className="flex h-8 items-center text-sm font-medium">{req.incoterm}</div></Field>
                <Field label={t("pq.field.incoterm_place_req", { incoterm: req.incoterm })} htmlFor="incoterm_place" error={errors.incoterm_place}>
                  <Input id="incoterm_place" value={form.incoterm_place} maxLength={120} aria-invalid={errors.incoterm_place ? "true" : undefined}
                    onChange={(e) => setField("incoterm_place", e.target.value)} />
                </Field>
              </div>
            )}
            <CargoFields value={form.cargo} onChange={(c) => change((f) => ({ ...f, cargo: { ...c, dims: f.cargo.dims } }))} errors={cargoErrors} dgDetails />
            <p className="mt-2 text-xs text-ink-tertiary">{t("pq.b.cargo.change_hint")}</p>
            <div className="mt-4">
              <DimsRows dims={form.cargo.dims} onChange={(d) => setField("cargo.dims", d)} errors={errors} t={t} locale={locale} volume={form.cargo.volume_m3} />
            </div>
            {option.warnings?.length > 0 && <WarningsList className="mt-4" warnings={option.warnings} dense />}
          </Section>

          <Section title={t("pq.b.sec.parties")}>
            <div className="flex flex-col gap-4">
              <PartyBlock k="shipper" title={t("pq.b.party.shipper")} hint={side === "shipper" ? t("pq.b.party.you") : null} value={form.shipper}
                onChange={(v) => setField("shipper", v)} errors={errors} locale={locale} t={t} mine={side === "shipper"} />
              <PartyBlock k="consignee" title={t("pq.b.party.consignee")} hint={side === "consignee" ? t("pq.b.party.you") : null} value={form.consignee}
                onChange={(v) => setField("consignee", v)} errors={errors} locale={locale} t={t} mine={side === "consignee"} />
              <div className="border-t border-[color:var(--border-subtle)] pt-3">
                <label className="flex items-center gap-2 text-sm">
                  <Checkbox checked={form.notifyOn} onCheckedChange={(v) => setField("notifyOn", !!v)} />
                  {t("pq.b.party.notify_add")}
                </label>
                {form.notifyOn && (
                  <div className="mt-3">
                    <PartyBlock k="notify" title={t("pq.b.party.notify")} value={form.notify} onChange={(v) => setField("notify", v)} errors={errors} locale={locale} t={t} />
                  </div>
                )}
              </div>
            </div>
          </Section>

          <Section title={t("pq.b.sec.pickup")}>
            <div className="flex flex-col gap-4">
              <StopBlock k="pickup" title={t("pq.b.stop.pickup")} value={form.pickup} onChange={(v) => setField("pickup", v)} party={form.shipper}
                partyLabel={partyLabel.shipper} errors={errors} locale={locale} t={t} required={hasPickup} sameLabel={t("pq.b.stop.same_shipper")}
                handover={t("pq.b.stop.handover_origin", { place: req.origin?.name || "—" })} />
              <StopBlock k="delivery" title={t("pq.b.stop.delivery")} value={form.delivery} onChange={(v) => setField("delivery", v)} party={form.consignee}
                partyLabel={partyLabel.consignee} errors={errors} locale={locale} t={t} required={hasDelivery} sameLabel={t("pq.b.stop.same_consignee")}
                handover={t("pq.b.stop.handover_destination", { place: req.destination?.name || "—" })} />
            </div>
          </Section>

          <Section title={t("pq.b.sec.refs")}>
            <div className="grid gap-x-3 gap-y-3 md:grid-cols-[16rem_minmax(0,1fr)]">
              <Field label={t("pq.b.f.customer_ref")} htmlFor="b-ref">
                <Input id="b-ref" value={form.customer_reference} maxLength={60} onChange={(e) => setField("customer_reference", e.target.value)} />
              </Field>
              <Field label={t("pq.b.f.po_numbers")} htmlFor="b-po" hint={t("pq.b.po.hint")}>
                <PoChips value={form.po_numbers} onChange={(v) => setField("po_numbers", v)} t={t} />
              </Field>
              <Field label={t("pq.b.f.special")} htmlFor="b-special" className="md:col-span-2">
                <Textarea id="b-special" rows={2} maxLength={2000} value={form.special_instructions} placeholder={t("pq.b.ph.special")}
                  onChange={(e) => setField("special_instructions", e.target.value)} />
              </Field>
            </div>
          </Section>
          {errCount > 0 && <p role="alert" className="mb-4 text-sm text-danger-fg lg:hidden">{t("pq.new.fix_n", { n: errCount })}</p>}
        </div>
        <aside aria-label={t("pq.b.card.title")} className="min-w-0">
          <div className="lg:sticky lg:top-[4.5rem]">
            {card}
            {errCount > 0 && <p role="alert" className="mt-2 hidden text-xs text-danger-fg lg:block">{t("pq.new.fix_n", { n: errCount })}</p>}
          </div>
        </aside>
      </div>
      {/* < 1024 px: total + confirm stay in reach while filling the long form */}
      {!blockedReason && (
        <div data-sticky-bar className="sticky bottom-0 z-20 -mx-4 mt-4 flex items-center justify-between gap-3 border-t border-[color:var(--border-subtle)] bg-[var(--bg-canvas)] px-4 py-2.5 lg:hidden">
          <div className="min-w-0">
            <p className="truncate text-xs text-ink-tertiary">{option.carrier_name}{savedAt ? ` · ${t("pq.b.draft_saved", { time: savedAt.toLocaleTimeString(locale === "fi" ? "fi-FI" : "en-GB", { hour: "2-digit", minute: "2-digit" }) })}` : ""}</p>
            <p className="text-sm tabular">{eur(option.total_cents, locale)} <span className="text-xs text-ink-tertiary">{t("quotes.opt.excl_vat")}{req.incoterm ? ` · ${req.incoterm}` : ""}</span></p>
          </div>
          <Button onClick={submit} disabled={submitting}>
            {submitting && <Loader2 className="animate-spin" />} {t("pq.b.confirm_short")}
          </Button>
        </div>
      )}
    </>
  );
}

function BookingSkeleton() {
  return (
    <div aria-busy="true" className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_21rem]">
      <div className="flex flex-col gap-3">
        <Skeleton className="h-4 w-48" />
        <Skeleton className="h-7 w-56" />
        {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-24 w-full rounded-lg" />)}
      </div>
      <Skeleton className="h-80 w-full rounded-xl" />
    </div>
  );
}

export default function BookingNew() {
  const { t } = useT();
  const [params] = useSearchParams();
  const quoteId = params.get("quote");
  const optionId = params.get("option");
  const q = useApi(quoteId ? `/quotes/${quoteId}` : null, { enabled: !!quoteId });

  if (!quoteId) {
    return (
      <EmptyState title={t("pq.b.no_quote")} body={t("pq.b.no_quote_body")}
        action={<Button size="sm" asChild><Link to="/portal/quotes">{t("nav.quotes")}</Link></Button>} />
    );
  }
  if (q.loading && !q.data) return <BookingSkeleton />;
  if (q.error && !q.data) return <ErrorState error={q.error} onRetry={q.reload} />;
  const quote = q.data?.quote;
  if (!quote) return null;
  const option = (quote.options || []).find((o) => o.id === optionId) || (quote.options || []).find((o) => o.id === quote.accepted_option_id);
  if (!option) {
    return (
      <EmptyState title={t("pq.b.no_option")} body={t("pq.b.no_option_body")}
        action={<Button size="sm" asChild><Link to={`/portal/quotes/${quote.id}`}>{t("pq.b.back_to_quote")}</Link></Button>} />
    );
  }
  return <BookingForm key={`${quote.id}-${option.id}`} quote={quote} option={option} />;
}
