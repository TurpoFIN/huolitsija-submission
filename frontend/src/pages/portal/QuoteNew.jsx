// Portal: request a quote. Decision: "what do I need priced?" -> route + cargo in one form, AI fill at the top.
import React, { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { DateInput } from "@/components/ui/DateInput";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { PageHeader, Section } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { AgentMark } from "@/components/app/StatusBadge";
import { apiGet, apiPost } from "@/lib/api";
import { date } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import LocationField from "@/features/quotes/LocationField";
import CargoFields from "@/features/quotes/CargoFields";
import ClaimNotice from "@/features/quotes/ClaimNotice";
import { Field } from "@/features/quotes/fields";
import { INCOTERMS, INSURANCE_COVERS, MODES, SEA_ONLY_INCOTERMS, cargoSummary, emptyCargo, normalizeLocation, routeLabel } from "@/features/quotes/quoteFormat";

const NONE = "__none";
const todayIso = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

function emptyForm() {
  return {
    origin: null, destination: null, mode: null, ready_date: "", incoterm: null, incoterm_place: "",
    cargo: emptyCargo(), customs_clearance: false, insurance: "NONE", notes: "",
  };
}

const hasLoc = (l) => !!(l && (l.unlocode || (l.query && l.query.trim())));

/** Merge a (partial) QuoteRequest into the form. Returns [form, filledKeys]. */
function mergeRequest(form, req, only) {
  if (!req) return [form, []];
  const next = { ...form, cargo: { ...form.cargo } };
  const filled = [];
  // `only` = the parser's `filled` list: keys it actually read from the text (others are defaults, e.g. stackable: false)
  const allowed = (k) => !Array.isArray(only) || only.includes(k);
  const setIf = (k, v) => {
    if (v === undefined || v === null || v === "" || !allowed(k)) return;
    next[k] = v;
    filled.push(k);
  };
  if (req.origin) setIf("origin", req.origin.unlocode ? normalizeLocation(req.origin) : req.origin);
  if (req.destination) setIf("destination", req.destination.unlocode ? normalizeLocation(req.destination) : req.destination);
  if (req.mode && MODES.includes(req.mode)) setIf("mode", req.mode);
  setIf("ready_date", req.ready_date ? String(req.ready_date).slice(0, 10) : null);
  if (req.incoterm && INCOTERMS.includes(req.incoterm)) setIf("incoterm", req.incoterm);
  setIf("incoterm_place", req.incoterm_place);
  if (typeof req.customs_clearance === "boolean") setIf("customs_clearance", req.customs_clearance);
  const cover = req.insurance?.cover || (typeof req.insurance === "string" ? req.insurance : null);
  if (cover && INSURANCE_COVERS.includes(cover)) setIf("insurance", cover);
  setIf("notes", req.notes);
  if (req.cargo && typeof req.cargo === "object") {
    Object.entries(req.cargo).forEach(([k, v]) => {
      if (v === undefined || v === null || v === "") return;
      if (Array.isArray(v) && v.length === 0) return;
      if (Array.isArray(only) && !only.includes(`cargo.${k}`)) return;
      next.cargo[k] = v;
      filled.push(`cargo.${k}`);
    });
    if (next.cargo.containers?.length && !req.cargo.load_type) next.cargo.load_type = "FCL";
  }
  return [next, filled];
}

function requestBody(f) {
  const c = { ...f.cargo };
  if (c.load_type === "FCL") {
    c.containers = (c.containers || []).filter((x) => x.type && x.count);
  } else {
    c.containers = [];
  }
  if (!c.dangerous_goods) c.dg = null;
  if (!c.description) delete c.description;
  return {
    mode: f.mode || null,
    origin: f.origin,
    destination: f.destination,
    ready_date: f.ready_date || null,
    incoterm: f.incoterm || null,
    incoterm_place: f.incoterm ? f.incoterm_place.trim() || null : null,
    cargo: c,
    insurance: { cover: f.insurance || "NONE" },
    customs_clearance: !!f.customs_clearance,
    notes: f.notes.trim() || null,
  };
}

function validate(f, t) {
  const e = {};
  if (!hasLoc(f.origin)) e.origin = t("pq.err.origin");
  if (!hasLoc(f.destination)) e.destination = t("pq.err.destination");
  if (!f.ready_date) e.ready_date = t("pq.err.ready_date");
  else if (f.ready_date < todayIso()) e.ready_date = t("pq.err.ready_past");
  if (f.incoterm && !f.incoterm_place.trim()) e.incoterm_place = t("pq.err.place");
  const c = f.cargo || {};
  if (c.load_type === "FCL") {
    const rows = (c.containers || []).filter((x) => x.type && x.count >= 1);
    if (!rows.length) e["cargo.containers"] = t("pq.err.containers");
  } else {
    if (!c.packages || c.packages < 1) e["cargo.packages"] = t("pq.err.packages");
    if (!c.weight_kg || c.weight_kg <= 0) e["cargo.weight_kg"] = t("pq.err.weight");
  }
  if (c.dangerous_goods) {
    if (!/^UN\d{4}$/.test((c.dg?.un_number || "").trim())) e["cargo.dg.un_number"] = t("pq.err.un_number");
    if (!c.dg?.class) e["cargo.dg.class"] = t("pq.err.dg_class");
  }
  if (f.insurance && f.insurance !== "NONE" && !(c.value_cents > 0)) e["cargo.value_cents"] = t("pq.err.value_for_insurance");
  return e;
}

const FIELD_IDS = {
  origin: "q-origin", destination: "q-destination", ready_date: "q-ready", incoterm: "q-incoterm", incoterm_place: "q-place",
  mode: "q-mode", weight_kg: "cargo-weight_kg", volume_m3: "cargo-volume_m3", packages: "cargo-packages", "cargo.weight_kg": "cargo-weight_kg",
  "cargo.volume_m3": "cargo-volume_m3", "cargo.packages": "cargo-packages", value_cents: "cargo-value_cents", "cargo.value_cents": "cargo-value_cents",
  "cargo.dg.un_number": "cargo-dg-un_number", "cargo.dg.class": "cargo-dg-class", insurance: "q-insurance",
  value_eur: "cargo-value_cents", "cargo.value_eur": "cargo-value_cents", description: "cargo-description", "cargo.description": "cargo-description",
  notes: "q-notes", customs_clearance: "q-customs",
};

function focusField(key) {
  const el = document.getElementById(FIELD_IDS[key] || key);
  if (el) {
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.focus({ preventScroll: true });
  }
}

const filled0 = (r) => Object.keys(r.request || {});

function AiBox({ onApply, onUnresolved, locale }) {
  const { t } = useT();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);

  const run = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await apiPost("/quotes/parse", { text: text.trim(), locale });
      const filled = onApply(r.request || {}, { ...(r.ai || {}), summary: r.summary || null, confidence: r.confidence ?? null,
        filled: r.filled || filled0(r), missing: r.missing || [], assumptions: r.assumptions || [] }, r.filled);
      setRes({ ...r, filledCount: filled.length });
      onUnresolved(Array.isArray(r.unresolved) ? r.unresolved : []);
    } catch (e) {
      setRes(null);
      setErr(e.status === 503 ? e.message || t("pq.ai.unavailable") : e.status === 0 ? t("state.offline") : e.message);
    } finally {
      setBusy(false);
    }
  };

  const fieldName = (f) => {
    const m = `pq.missing.${f}`;
    if (t(m) !== m) return t(m);
    const k = `pq.field.${f}`;
    const s = t(k);
    return s === k ? f.replace(/^cargo\./, "").replace(/_/g, " ") : s;
  };

  return (
    <section aria-labelledby="ai-title" className="mb-6 rounded-xl px-4 py-3 shadow-[inset_0_0_0_0.5px_var(--border-default)]">
      <div className="mb-2 flex items-center gap-2">
        <h2 id="ai-title" className="text-sm font-medium">{t("pq.ai.title")}</h2>
        <AgentMark />
      </div>
      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run();
        }}
        rows={3}
        placeholder={t("pq.ai.placeholder")}
        aria-label={t("pq.ai.title")}
        maxLength={4000}
      />
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <Button variant="secondary" onClick={run} disabled={busy || !text.trim()}>
          {busy ? <Loader2 className="animate-spin" /> : <Sparkles />}
          {busy ? t("pq.ai.working") : t("pq.ai.fill")}
        </Button>
        <span className="text-xs text-ink-tertiary">{t("pq.ai.hint")}</span>
      </div>
      {err && (
        <p role="alert" className="mt-2 text-sm text-ink-secondary">
          {err} <span className="text-ink-tertiary">{t("pq.ai.form_still_works")}</span>
        </p>
      )}
      {res && (
        <div className="mt-3 flex flex-col gap-2 border-t border-[color:var(--border-subtle)] pt-3 text-sm" role="status">
          <p className="text-ink-secondary">
            {t("pq.ai.filled", { n: res.filledCount })}
            {res.summary ? <span className="text-ink-tertiary"> · {res.summary}</span> : null}
          </p>
          {Array.isArray(res.missing) && res.missing.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-ink-secondary">{t("pq.ai.missing")}:</span>
              {res.missing.map((m) => (
                <Button key={m} size="xs" variant="outline" onClick={() => focusField(m)}>{fieldName(m)}</Button>
              ))}
            </div>
          )}
          {Array.isArray(res.assumptions) && res.assumptions.length > 0 && (
            <div>
              <span className="text-ink-secondary">{t("pq.ai.assumptions")}:</span>
              <ul className="ml-4 list-disc text-ink-tertiary">
                {res.assumptions.map((a, i) => <li key={i}>{a}</li>)}
              </ul>
            </div>
          )}
          {Array.isArray(res.unresolved) && res.unresolved.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-ink-secondary">{t("pq.ai.check_place")}:</span>
              {res.unresolved.map((u, i) => (
                <Button key={`${u.field}-${i}`} size="xs" variant="outline" onClick={() => focusField(u.field)}>
                  {fieldName(u.field)}{u.query ? (locale === "fi" ? ` ”${u.query}”` : ` “${u.query}”`) : ""}
                </Button>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

/** One-click place choices under a location field when the AI could not pin the place down (parse `unresolved[]`). */
function PlaceSuggestions({ items, onPick }) {
  const { t } = useT();
  if (!items.length) return null;
  return items.map((u, k) => {
    const cands = Array.isArray(u.candidates) ? u.candidates.map(normalizeLocation).filter(Boolean) : [];
    return (
      <div key={k} className="flex flex-wrap items-center gap-1.5 text-xs" role="group" aria-label={t("pq.ai.did_you_mean")}>
        <AgentMark />
        {cands.length === 0 ? (
          <span className="text-ink-secondary">{t("pq.ai.no_match", { q: u.query || "" })}</span>
        ) : (
          <>
            <span className="text-ink-secondary">{t("pq.ai.did_you_mean")}</span>
            {cands.slice(0, 4).map((c, i) => (
              <Button key={`${c.unlocode}-${i}`} size="xs" variant="outline" onClick={() => onPick(c)}>
                {c.name}
                {c.unlocode ? <span className="font-mono text-[10px] text-ink-tertiary">{c.unlocode}</span> : null}
              </Button>
            ))}
          </>
        )}
      </div>
    );
  });
}

function RequestSummary({ form, errCount, submitting, serverError }) {
  const { t, locale } = useT();
  const cargo = cargoSummary(form.cargo, t, locale);
  const services = [form.customs_clearance ? t("pq.detail.customs_yes") : null,
    form.insurance && form.insurance !== "NONE" ? t(`quotes.insurance.${form.insurance}`) : null].filter(Boolean).join(" · ");
  const rows = [
    { k: "route", label: t("pq.col.route"), ok: hasLoc(form.origin) && hasLoc(form.destination), req: true,
      value: form.origin || form.destination ? routeLabel(form.origin, form.destination, { short: true }) : null },
    { k: "mode", label: t("field.mode"), ok: true, value: form.mode ? t(`enum.mode.${form.mode}`) : t("quotes.mode.any") },
    { k: "ready", label: t("pq.field.ready_date"), ok: !!form.ready_date, req: true, value: form.ready_date ? date(form.ready_date, locale) : null },
    { k: "incoterm", label: t("field.incoterm"), ok: true, value: form.incoterm ? `${form.incoterm} ${form.incoterm_place || ""}`.trim() : null },
    { k: "cargo", label: t("pq.col.cargo"), ok: cargo !== "—", req: true, value: cargo !== "—" ? cargo : null },
    { k: "services", label: t("pq.detail.services"), ok: true, value: services || null },
  ];
  const missing = rows.filter((r) => r.req && !r.ok).length;
  return (
    <div className="flex flex-col gap-3 rounded-xl bg-surface px-4 py-3 shadow-[inset_0_0_0_0.5px_var(--border-default)]">
      <p className="text-sm font-medium">{missing ? t("pq.sum.missing_n", { n: missing }) : t("pq.sum.ready")}</p>
      <dl className="flex flex-col gap-1.5 text-sm">
        {rows.map((r) => (
          <div key={r.k} className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-2">
            <dt className="text-ink-tertiary">{r.label}</dt>
            <dd className={cn("min-w-0 break-words", !r.value && "text-ink-tertiary")}>
              {r.value || (r.req ? t("pq.sum.required") : "—")}
            </dd>
          </div>
        ))}
      </dl>
      <Button type="submit" form="quote-form" disabled={submitting}>
        {submitting && <Loader2 className="animate-spin" />}
        {submitting ? t("pq.new.pricing") : t("pq.new.submit")}
      </Button>
      <p className={cn("text-xs", errCount ? "text-danger-fg" : "text-ink-tertiary")}>
        {errCount ? t("pq.new.fix_n", { n: errCount }) : t("pq.new.submit_hint")}
      </p>
      {serverError && <p role="alert" className="rounded-lg px-3 py-2 text-sm status-failed">{serverError}</p>}
    </div>
  );
}

export default function QuoteNew() {
  const { t, locale } = useT();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const fromId = params.get("from");
  const [form, setForm] = useState(emptyForm);
  const [ai, setAi] = useState(() => new Set());
  const [errors, setErrors] = useState({});
  const [tried, setTried] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState(null);
  const [prefill, setPrefill] = useState(null); // {number} | {error}
  const [aiMeta, setAiMeta] = useState(null); // parse result kept with the quote (source "ai")
  const loadedFrom = useRef(null);

  // "Edit and requote": copy the request of an existing quote.
  useEffect(() => {
    if (!fromId || loadedFrom.current === fromId) return;
    loadedFrom.current = fromId;
    apiGet(`/quotes/${fromId}`)
      .then((r) => {
        const [next] = mergeRequest(emptyForm(), r.quote?.request);
        if (next.ready_date && next.ready_date < todayIso()) next.ready_date = "";
        setForm(next);
        setPrefill({ number: r.quote?.number });
      })
      .catch((e) => setPrefill({ error: e.message }));
  }, [fromId]);

  const update = (patch, keys) => {
    setForm((f) => {
      const next = { ...f, ...patch };
      if (tried) setErrors(validate(next, t));
      return next;
    });
    if (keys && keys.length) setAi((s) => {
      const n = new Set(s);
      keys.forEach((k) => n.delete(k));
      return n;
    });
  };

  const setCargo = (cargo) => {
    setForm((f) => {
      const changed = Object.keys(cargo).filter((k) => JSON.stringify(cargo[k]) !== JSON.stringify(f.cargo?.[k]));
      if (changed.length) setAi((s) => {
        const n = new Set(s);
        changed.forEach((k) => n.delete(`cargo.${k}`));
        if (changed.includes("dg")) Array.from(n).filter((k) => k.startsWith("cargo.dg.")).forEach((k) => n.delete(k));
        return n;
      });
      const next = { ...f, cargo };
      if (tried) setErrors(validate(next, t));
      return next;
    });
  };

  const [unresolved, setUnresolved] = useState([]);
  const unresolvedFor = (field) => unresolved.filter((u) => u.field === field);
  const pickPlace = (field, loc) => {
    applyAi({ [field]: loc }, null, [field]);
    setUnresolved((us) => us.filter((u) => u.field !== field));
  };

  const applyAi = (req, meta, only) => {
    if (meta) setAiMeta(meta);
    setForm((f) => {
      const [next] = mergeRequest(f, req, only);
      if (tried) setErrors(validate(next, t));
      return next;
    });
    const [, filled] = mergeRequest(emptyForm(), req, only); // which keys the AI supplied (independent of current values)
    setAi((s) => new Set([...s, ...filled]));
    return filled;
  };

  const cargoErrors = useMemo(() => {
    const out = {};
    Object.entries(errors).forEach(([k, v]) => {
      if (k.startsWith("cargo.")) out[k.slice(6)] = v;
    });
    return out;
  }, [errors]);
  const cargoAi = useMemo(() => Array.from(ai).filter((k) => k.startsWith("cargo.")).map((k) => k.slice(6)), [ai]);
  const mark = (k) => (ai.has(k) ? <AgentMark /> : null);

  const submit = async (e) => {
    e?.preventDefault?.();
    setTried(true);
    const errs = validate(form, t);
    setErrors(errs);
    setServerError(null);
    const first = Object.keys(errs)[0];
    if (first) {
      focusField(first);
      return;
    }
    setSubmitting(true);
    try {
      const r = await apiPost("/quotes", { ...requestBody(form), ...(aiMeta ? { ai: aiMeta } : {}) });
      navigate(`/portal/quotes/${r.quote.id}`);
    } catch (err) {
      setServerError(err.status === 0 ? t("state.offline") : err.message);
      setSubmitting(false);
    }
  };

  const seaOnlyClash = form.incoterm && SEA_ONLY_INCOTERMS.includes(form.incoterm) && form.mode && form.mode !== "sea";
  const modeOptions = [{ value: null, label: t("quotes.mode.any") }, ...MODES.map((m) => ({ value: m, label: t(`enum.mode.${m}`) }))];
  const errCount = Object.keys(errors).length;

  return (
    <div className="max-w-[1240px]">
      <PageHeader
        crumbs={[{ label: t("nav.quotes"), to: "/portal/quotes" }, { label: t("pq.new.crumb") }]}
        title={t("pq.new.title")}
        meta={<span>{t("pq.new.meta")}</span>}
      />
      <ClaimNotice />
      {prefill?.number && <p className="mb-3 text-sm text-ink-secondary">{t("pq.new.prefilled", { number: prefill.number })}</p>}
      {prefill?.error && <p role="alert" className="mb-3 text-sm text-danger-fg">{prefill.error}</p>}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_19rem]">
      <div className="min-w-0">
      <AiBox onApply={applyAi} onUnresolved={setUnresolved} locale={locale} />

      <form id="quote-form" onSubmit={submit} noValidate>
        <Section title={t("pq.sec.route")}>
          <div className="grid gap-x-3 gap-y-3 md:grid-cols-2">
            <div className="flex min-w-0 flex-col gap-1">
              <LocationField id="q-origin" label={t("field.origin")} placeholder={t("pq.ph.origin")} value={form.origin} mode={form.mode}
                onChange={(v) => { update({ origin: v }, ["origin"]); setUnresolved((us) => us.filter((u) => u.field !== "origin")); }}
                invalid={!!errors.origin} mark={mark("origin")} aria-describedby={errors.origin ? "q-origin-err" : undefined} />
              <PlaceSuggestions items={unresolvedFor("origin")} onPick={(loc) => pickPlace("origin", loc)} />
              {errors.origin && <p id="q-origin-err" role="alert" className="text-xs text-danger-fg">{errors.origin}</p>}
            </div>
            <div className="flex min-w-0 flex-col gap-1">
              <LocationField id="q-destination" label={t("field.destination")} placeholder={t("pq.ph.destination")} value={form.destination}
                mode={form.mode} onChange={(v) => { update({ destination: v }, ["destination"]); setUnresolved((us) => us.filter((u) => u.field !== "destination")); }}
                invalid={!!errors.destination} mark={mark("destination")} aria-describedby={errors.destination ? "q-destination-err" : undefined} />
              <PlaceSuggestions items={unresolvedFor("destination")} onPick={(loc) => pickPlace("destination", loc)} />
              {errors.destination && <p id="q-destination-err" role="alert" className="text-xs text-danger-fg">{errors.destination}</p>}
            </div>
          </div>
          <div className="mt-3 grid gap-x-3 gap-y-3 md:grid-cols-[minmax(0,1fr)_12rem]">
            <Field label={t("field.mode")} mark={mark("mode")}>
              <div id="q-mode">
                <Segmented label={t("field.mode")} value={form.mode} onChange={(v) => update({ mode: v }, ["mode"])} options={modeOptions} />
              </div>
            </Field>
            <Field label={t("pq.field.ready_date")} htmlFor="q-ready" error={errors.ready_date} mark={mark("ready_date")}>
              <DateInput id="q-ready" min={todayIso()} value={form.ready_date || ""} aria-invalid={errors.ready_date ? "true" : undefined}
                aria-describedby={errors.ready_date ? "q-ready-err" : undefined} onChange={(iso) => update({ ready_date: iso }, ["ready_date"])} />
            </Field>
          </div>
        </Section>

        <Section title={t("pq.sec.terms")}>
          <div className="grid gap-x-3 gap-y-3 md:grid-cols-[16rem_minmax(0,1fr)]">
            <Field label={t("field.incoterm")} htmlFor="q-incoterm" mark={mark("incoterm")}>
              <Select value={form.incoterm || undefined} onValueChange={(v) => update({ incoterm: v === NONE ? null : v }, ["incoterm"])}>
                <SelectTrigger id="q-incoterm">
                  <SelectValue placeholder={t("pq.ph.incoterm")}>
                    {form.incoterm ? `${form.incoterm} – ${t(`pq.incoterm.name.${form.incoterm}`)}` : undefined}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent className="max-w-[min(28rem,calc(100vw-2rem))]">
                  {INCOTERMS.map((code) => (
                    <SelectItem key={code} value={code} className="items-start py-2">
                      <span className="flex flex-col">
                        <span><span className="font-medium">{code}</span> <span className="text-ink-secondary">{t(`pq.incoterm.name.${code}`)}</span></span>
                        <span className="whitespace-normal text-xs text-ink-tertiary">{t(`quotes.incoterm.${code}`)}</span>
                      </span>
                    </SelectItem>
                  ))}
                  <SelectItem value={NONE} className="py-2">{t("pq.incoterm.not_agreed")}</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field label={form.incoterm ? t("pq.field.incoterm_place_req", { incoterm: form.incoterm }) : t("pq.field.incoterm_place")}
              htmlFor="q-place" error={errors.incoterm_place} mark={mark("incoterm_place")}>
              <Input id="q-place" value={form.incoterm_place} disabled={!form.incoterm} placeholder={form.incoterm ? t("pq.ph.place") : t("pq.ph.place_first")}
                aria-invalid={errors.incoterm_place ? "true" : undefined} maxLength={120}
                onChange={(e) => update({ incoterm_place: e.target.value }, ["incoterm_place"])} />
            </Field>
          </div>
          <p className="mt-2 text-xs text-ink-tertiary">
            {form.incoterm ? t(`quotes.incoterm.${form.incoterm}`) : t("pq.incoterm.none_hint")}
          </p>
          {seaOnlyClash && <p className="mt-1 text-xs text-warning-fg">{t("quotes.warn.incoterm.sea_only", { incoterm: form.incoterm })}</p>}
        </Section>

        <Section title={t("pq.sec.cargo")}>
          <CargoFields value={form.cargo} onChange={setCargo} errors={cargoErrors} aiFields={cargoAi} />
        </Section>

        <Section title={t("pq.sec.services")}>
          <div className="flex flex-col gap-4">
            <div>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox aria-label={t("pq.field.customs")} id="q-customs" checked={form.customs_clearance} onCheckedChange={(v) => update({ customs_clearance: !!v }, ["customs_clearance"])} />
                {t("pq.field.customs")}
                {mark("customs_clearance")}
              </label>
              <p className="ml-6 mt-0.5 text-xs text-ink-tertiary">{t("pq.field.customs_hint")}</p>
            </div>
            <div className="grid gap-3 md:grid-cols-[16rem_minmax(0,1fr)]">
              <Field label={t("pq.field.insurance")} htmlFor="q-insurance" mark={mark("insurance")}
                error={errors["cargo.value_cents"] && form.insurance !== "NONE" ? t("pq.err.value_for_insurance") : null}>
                <Select value={form.insurance} onValueChange={(v) => update({ insurance: v }, ["insurance"])}>
                  <SelectTrigger id="q-insurance"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {INSURANCE_COVERS.map((c) => <SelectItem key={c} value={c}>{t(`quotes.insurance.${c}`)}</SelectItem>)}
                  </SelectContent>
                </Select>
              </Field>
              <p className="self-end pb-1.5 text-xs text-ink-tertiary">
                {form.insurance === "NONE" ? t("pq.insurance.none_hint") : t("pq.insurance.hint")}
              </p>
            </div>
            <Field label={t("pq.field.notes")} htmlFor="q-notes" mark={mark("notes")}>
              <Textarea id="q-notes" rows={2} value={form.notes} maxLength={2000} placeholder={t("pq.ph.notes")}
                onChange={(e) => update({ notes: e.target.value }, ["notes"])} />
            </Field>
          </div>
        </Section>

        <div data-sticky-bar className="sticky bottom-0 z-10 -mx-4 flex flex-wrap items-center gap-3 border-t border-[color:var(--border-subtle)] bg-[var(--bg-canvas)] px-4 py-3 md:static md:mx-0 md:border-0 md:bg-transparent md:px-0 xl:hidden">
          <Button type="submit" disabled={submitting}>
            {submitting && <Loader2 className="animate-spin" />}
            {submitting ? t("pq.new.pricing") : t("pq.new.submit")}
          </Button>
          <Button variant="ghost" asChild><Link to="/portal/quotes">{t("action.cancel")}</Link></Button>
          <span className={cn("text-xs", errCount ? "text-danger-fg" : "text-ink-tertiary")} role={errCount ? "alert" : undefined}>
            {errCount ? t("pq.new.fix_n", { n: errCount }) : t("pq.new.submit_hint")}
          </span>
          {serverError && (
            <p role="alert" className="w-full rounded-lg px-3 py-2 text-sm status-failed">{serverError}</p>
          )}
        </div>
      </form>
      </div>
      <aside className="hidden xl:block" aria-label={t("pq.sum.title")}>
        <div className="sticky top-[4.5rem]">
          <RequestSummary form={form} errCount={errCount} submitting={submitting} serverError={serverError} />
        </div>
      </aside>
      </div>
    </div>
  );
}
