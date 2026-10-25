// Normalises the s7 commercial API payloads (routes/{crm,tenders,pricing_rules,credit,insurance}.py) into the
// shapes the tabs render. Pure functions, no React: each tolerates the documented variants (list or {items}),
// keeps missing values as null (shown as "—") and never invents a number.

export const isBlank = (v) => v === null || v === undefined || v === "";

/** [] | {items: []} | {leads: []} ... -> [] */
export function listOf(data, ...keys) {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  for (const k of ["items", ...keys]) if (Array.isArray(data[k])) return data[k];
  return [];
}

/** Bilingual text: {fi, en} | {text_fi, text} | "plain". */
export function bi(v, locale, base) {
  if (v === null || v === undefined) return null;
  if (typeof v === "string") return v;
  if (base) {
    const fi = v[`${base}_fi`];
    const en = v[`${base}_en`] ?? v[base];
    return locale === "fi" ? fi ?? en ?? null : en ?? fi ?? null;
  }
  return locale === "fi" ? v.fi ?? v.en ?? v.text ?? v.message ?? null : v.en ?? v.text ?? v.message ?? v.fi ?? null;
}

/** Reasons arrive as strings or {code, fi, en, source_url|url|source}. */
export function reasons(list, locale) {
  return (Array.isArray(list) ? list : []).map((r, i) => {
    if (typeof r === "string") return { key: `${i}`, text: r, url: null, source: null, points: null };
    return {
      key: r.code || `${i}`,
      text: bi(r, locale) ?? bi(r, locale, "message") ?? bi(r, locale, "label") ?? r.code ?? "",
      url: r.source_url || r.url || null,
      source: r.source_name || (typeof r.source === "string" ? r.source : r.source?.name) || null,
      points: r.points ?? r.weight ?? null,
    };
  });
}

// ---- CRM ---------------------------------------------------------------------------------------------------------
// Two axes (services/crm.py): a lead has a `status` (found -> contacted -> replied ...), its opportunity a `stage`.

export const STAGE_ORDER = ["prospect", "contacted", "replied", "qualified", "quoting", "tender", "negotiation", "won", "lost"];
export const LEAD_STATUSES = ["awaiting_person", "replied", "qualified", "contactable", "contacted", "nurture", "scored", "new",
  "no_contact", "converted", "disqualified", "opted_out"];
const CLOSED = new Set(["won", "lost"]);
export const isOpenStage = (s) => !CLOSED.has(s);
/** Lead statuses that wait for a person: a draft the agent may not send, or a reply. */
export const NEEDS_PERSON = new Set(["awaiting_person", "replied"]);

/** GET /crm/pipeline -> [{stage, count, value_cents}] ordered by the funnel. */
export function pipelineStages(data) {
  if (!data) return [];
  let rows = listOf(data, "stages");
  if (!rows.length && data.stages && typeof data.stages === "object") {
    rows = Object.entries(data.stages).map(([stage, v]) => (typeof v === "number" ? { stage, count: v } : { stage, ...v }));
  }
  const out = rows.map((r) => ({
    stage: r.stage || r.key || r.id,
    count: r.count ?? r.n ?? 0,
    value_cents: r.estimated_annual_value_cents ?? r.value_cents ?? r.annual_value_cents ?? null,
  })).filter((r) => r.stage);
  const idx = (s) => (STAGE_ORDER.indexOf(s) === -1 ? 99 : STAGE_ORDER.indexOf(s));
  return out.sort((a, b) => idx(a.stage) - idx(b.stage));
}

export function pipelineTotals(data, stages) {
  const open = stages.filter((s) => isOpenStage(s.stage));
  const sum = (arr, k) => (arr.some((s) => !isBlank(s[k])) ? arr.reduce((a, s) => a + (s[k] || 0), 0) : null);
  const leads = data?.leads && typeof data.leads === "object" ? data.leads : {};
  return {
    open_count: data?.open_count ?? sum(open, "count") ?? 0,
    open_value_cents: data?.open_estimated_annual_value_cents ?? data?.open_value_cents ?? sum(open, "value_cents"),
    follow_ups_due: data?.follow_ups_due ?? null,
    lead_counts: leads,
    leads_total: Object.values(leads).reduce((a, n) => a + (n || 0), 0),
  };
}

const sourceName = (url) => (!url ? null : /eurostat|comext/i.test(url) ? "Eurostat" : /prh|ytj/i.test(url) ? "PRH/YTJ" : null);

/** A lead row (crm_leads + lead_detail extras). */
export function lead(l, locale) {
  if (!l) return null;
  const src = typeof l.source === "string" ? { source: l.source } : l.source || {};
  const reasonsList = reasons(l.score_reasons, locale).map((r) => ({ ...r, source: r.source || sourceName(r.url) }));
  const opp = l.opportunity || null;
  return {
    ...l,
    id: l.id,
    name: l.name || "—",
    business_id: l.business_id || null,
    city: l.address?.city || l.city || null,
    industry: (locale === "fi" ? l.tol_label_fi || l.tol_label : l.tol_label || l.tol_label_fi) || null,
    industry_code: l.tol_code || null,
    status: l.status || "new",
    stage: opp?.stage || null,
    score: l.score ?? null,
    score_reasons: reasonsList,
    source_kind: l.origin || (/prh|ytj/i.test(src.source || "") ? "prh" : src.source) || null,
    source_url: src.url || null,
    trade_url: reasonsList.find((r) => r.source === "Eurostat")?.url || null,
    value_cents: opp?.estimated_annual_value_cents ?? null,
    next_follow_up_at: l.next_follow_up_at || null,
    last_contacted_at: l.last_touch_at || null,
    opted_out: l.status === "opted_out",
    customer_id: l.company_id || null,
    opportunity: opp,
    draft: l.draft || null,
    website: l.website || null,
    ref: l.ref || null,
  };
}

export function contact(c, locale) {
  const lb = c.lawful_basis;
  return {
    ...c,
    email: c.email || null,
    role: c.role || null,
    lawful_basis: lb && typeof lb === "object" ? bi(lb, locale) : lb || null,
    lawful_basis_code: lb && typeof lb === "object" ? lb.code : null,
    source_url: c.source_url || null,
    opted_out: c.status === "opted_out" || !!c.opted_out,
    status: c.status || "active",
  };
}

export function activity(a, locale) {
  return {
    ...a,
    kind: a.kind || "note",
    at: a.at || null,
    subject: a.subject || null,
    summary: bi(a, locale, "summary") || null,
    intent: a.intent || a.data?.intent || null,
    status: a.status || null,
    actor: a.actor?.type === "agent" ? "agent" : a.actor?.name || null,
    sandbox: !!a.sandbox,
  };
}

/** GET /crm/status -> agent strip fields. */
export function crmStatus(s) {
  if (!s) return null;
  const lc = s.last_cycle || null;
  return {
    autopilot: s.autopilot_enabled ?? null,
    halted: !!s.halted,
    sending: s.sending ?? null,
    sends_today: s.sends_today ?? null,
    daily_limit: s.daily_send_limit ?? s.daily_limit ?? null,
    last_cycle_at: lc ? lc.finished_at || lc.started_at || null : null,
    last_cycle: lc,
    email_provider: s.email?.provider || null,
    email_live: s.email?.live ?? null,
    sandbox_domain: s.email?.sandbox_domain || null,
    imap_configured: s.imap?.configured ?? null,
    schedule: s.schedules?.["crm.prospect_cycle"] || null,
    counts: s.counts || {},
  };
}

// ---- Tenders -----------------------------------------------------------------------------------------------------

const PRICE_UNIT = { per_kg: "kg", per_container: "container", per_shipment: "shipment", per_trailer: "trailer",
  per_ldm: "ldm", per_pallet: "pallet", per_wm: "cbm" };
const VOLUME_UNIT = { shipments: "shipment", containers: "container", teu: "teu", pallets: "pallet", tons: "ton", kg: "kg", trailers: "trailer" };

function tenderPlace(p) {
  if (!p) return null;
  if (typeof p === "string") return { name: p };
  return { ...p, unlocode: p.unlocode || p.locode || null, name: p.name || p.city || p.text || null,
    country: p.country || null };
}

// Tender lanes from services/tenders.py: {parsed{origin, destination, mode}, annual{volume, unit}, best{price_per_unit_cents,
// unit, annual_value_cents, firmness, service}, status, reason(_fi), warnings[{message, message_fi}]}; tariff lanes are flat.
export function tenderLane(l) {
  const parsed = l.parsed || {};
  const best = l.best || {};
  const annual = l.annual || {};
  return {
    ...l,
    origin: tenderPlace(l.origin || parsed.origin),
    destination: tenderPlace(l.destination || parsed.destination),
    mode: l.mode || best.mode || parsed.mode || null,
    service: l.service || best.service || parsed.service || null,
    annual_volume: annual.volume ?? l.annual_volume ?? l.volume ?? null,
    volume_unit: VOLUME_UNIT[annual.unit] || annual.unit || l.volume_unit || null,
    price_per_unit_cents: best.price_per_unit_cents ?? l.price_per_unit_cents ?? l.unit_price_cents ?? l.price_cents ?? null,
    price_unit: PRICE_UNIT[best.unit] || PRICE_UNIT[(l.basis || "").toLowerCase()] || null,
    annual_value_cents: best.annual_value_cents ?? l.annual_value_cents ?? null,
    firmness: best.price_basis === "contract" ? "contract" : best.firmness || l.firmness || null,
    warnings: [
      ...(l.status && l.status !== "priced" && (l.reason || l.reason_fi) ? [{ message: l.reason, message_fi: l.reason_fi }] : []),
      ...(l.warnings || []).map((w) => (typeof w === "string" ? { text: w } : w)),
    ],
  };
}

export function tender(t) {
  if (!t) return null;
  const lanes = (t.lanes || []).map(tenderLane);
  const sum = lanes.some((l) => !isBlank(l.annual_value_cents)) ? lanes.reduce((a, l) => a + (l.annual_value_cents || 0), 0) : null;
  return {
    ...t,
    name: t.number || t.name || t.title || t.original_filename || t.filename || t.id,
    customer: t.company_name || t.customer_name || t.company?.name || null,
    status: t.status || "uploaded",
    lanes,
    lane_count: t.totals?.lanes ?? t.lane_count ?? lanes.length,
    priced_count: t.totals?.priced ?? lanes.filter((l) => !isBlank(l.price_per_unit_cents)).length,
    total_annual_cents: t.totals?.annual_value_cents ?? t.total_annual_value_cents ?? t.annual_value_cents ?? sum,
    due_at: t.due_at || t.deadline || t.due_date || null,
    file_name: t.original_filename || t.filename || t.file_name || null,
    tariff_id: t.tariff_id || t.contract_tariff_id || null,
  };
}

// ---- Tariffs -----------------------------------------------------------------------------------------------------

export function tariff(t) {
  if (!t) return null;
  return {
    ...t,
    name: t.name || t.title || t.reference || t.id,
    customer: t.company_name || t.customer_name || t.company?.name || null,
    company_id: t.company_id || t.company?.id || null,
    status: t.status || "draft",
    valid_from: t.valid_from || null,
    valid_to: t.valid_to || null,
    lanes: (t.lanes || []).map(tenderLane),
    lane_count: t.lane_count ?? (t.lanes || []).length,
    tender_id: t.tender_id || null,
  };
}

// ---- Margin rules ------------------------------------------------------------------------------------------------

export function marginRule(r) {
  const m = r.match || r.scope || {};
  const pick = (k) => r[k] ?? m[k] ?? null;
  return {
    ...r,
    name: r.name || null,
    mode: pick("mode"),
    service: pick("service"),
    lane: pick("lane") || (pick("origin") || pick("destination") ? `${pick("origin") || "*"}>${pick("destination") || "*"}` : null),
    company_id: pick("company_id"),
    company_name: r.company_name || m.company_name || null,
    size_min: pick("size_min") ?? pick("min_size"),
    size_max: pick("size_max") ?? pick("max_size"),
    size_unit: pick("size_unit") || pick("size_basis") || null,
    margin_pct: r.margin_pct ?? null,
    floor_cents: r.floor_cents ?? r.min_margin_cents ?? null,
    priority: r.priority ?? 0,
    active: r.active !== false && r.enabled !== false,
  };
}

/** Simulation result: {options:[...]} | {results:[...]} | [] -> per option rows. */
export function simulation(data) {
  const rows = listOf(data, "options", "results");
  return rows.map((o, i) => {
    const res = o.margin || o.resolved || o.rule || {};
    return {
      key: o.id || o.option_id || `${i}`,
      label: o.label || o.carrier_name || o.service || o.mode || `#${i + 1}`,
      mode: o.mode || null,
      cost_cents: o.cost_cents ?? o.buy_cents ?? null,
      sell_cents: o.sell_cents ?? o.price_cents ?? o.total_cents ?? null,
      margin_pct: o.margin_pct ?? res.margin_pct ?? null,
      floor_cents: o.floor_cents ?? res.floor_cents ?? null,
      rule_id: o.rule_id ?? o.margin_rule_id ?? res.rule_id ?? null,
      rule_name: o.rule_name ?? res.rule_name ?? null,
      source: o.source ?? res.source ?? null,
      floor_applied: !!(o.floor_applied || o.raised_to_floor),
    };
  });
}

// ---- Credit ------------------------------------------------------------------------------------------------------

export function creditPosition(p) {
  if (!p) return null;
  const pos = p.position || p;
  const limit = pos.limit_cents ?? pos.credit_limit_cents ?? null;
  const open = pos.open_ar_cents ?? null;
  const booked = pos.booked_uninvoiced_cents ?? null;
  const available = pos.available_cents ?? (limit !== null && open !== null ? limit - open - (booked || 0) : null);
  const last = pos.last_check || p.last_check || p.credit_last_check || p.latest_check || null;
  return {
    ...pos,
    ...p,
    company_id: pos.company_id || p.company_id || p.id || null,
    name: p.name || p.company_name || pos.company_name || p.company?.name || null,
    business_id: pos.business_id || p.business_id || p.company?.business_id || null,
    hold_reason: pos.hold_reason ?? p.hold_reason ?? null,
    limit_basis: pos.limit_basis || null,
    used_pct: pos.used_pct ?? null,
    max_days_overdue: pos.max_days_overdue ?? null,
    exposure_cents: pos.exposure_cents ?? null,
    limit_cents: limit,
    open_ar_cents: open,
    overdue_cents: pos.overdue_cents ?? null,
    booked_uninvoiced_cents: booked,
    available_cents: available,
    utilisation: limit ? Math.max(0, (limit - (available ?? limit)) / limit) : null,
    hold: !!(pos.hold ?? p.credit_hold),
    status: pos.status || p.credit_status || (pos.hold ? "hold" : "ok"),
    last_check: last,
  };
}

export function creditCheck(c, locale) {
  if (!c) return null;
  return {
    ...c,
    at: c.at || c.created_at || c.checked_at || null,
    rating: c.rating ?? c.grade ?? null,
    score: c.score ?? null,
    recommended_limit_cents: c.recommended_limit_cents ?? c.suggested_limit_cents ?? null,
    reasons: reasons(c.reasons || c.signals, locale),
    sources: (c.sources || []).map((s) => (typeof s === "string" ? { name: s, url: null } : { name: s.name || s.source || s.kind, label: s.label || null, url: s.url || null, status: s.status || null })),
    provider: c.provider || c.bureau || null,
    check_status: c.status || null,
    outcome: c.outcome || null,
  };
}

// ---- Insurance ---------------------------------------------------------------------------------------------------

/** GET /insurance/providers -> [{id, name, status: live|live_ready|not_configured, blocked_on(_fi), active}] */
export function providers(data) {
  const rows = listOf(data, "providers");
  return rows.map((p) => ({
    ...p,
    id: p.id || p.key || p.name,
    name: p.name || p.label || p.id,
    status: p.state || p.status || (p.configured === false ? "not_configured" : p.mode) || "not_configured",
    active: !!(p.active ?? p.selected ?? (data && data.active === (p.id || p.key))),
  }));
}

export function policy(p) {
  return {
    ...p,
    provider: p.provider || p.provider_id || null,
    certificate_number: p.certificate_number || p.certificate_no || null,
    certificate_url: p.certificate_url || null,
    status: p.status || "bound",
    premium_cents: p.premium_cents ?? null,
    insured_value_cents: p.insured_value_cents ?? null,
    cover: p.cover || null,
    shipment_id: p.shipment_id || null,
    shipment_reference: p.shipment_reference || p.reference || null,
    at: p.bound_at || p.created_at || null,
  };
}
