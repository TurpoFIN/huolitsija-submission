/**
 * Public quote form state <-> URL query <-> QuoteRequest (api-contract.md).
 *
 * URL keys: o/ol origin (UN/LOCODE or free text, display label), d/dl destination, m mode, lt load type,
 * n packages, pt package type, w weight kg, v volume m³, st stackable (0/1), dg (1), c containers "40HC:1,20GP:2",
 * r ready date, i incoterm, ip incoterm place, cc customs clearance (0/1), ins insurance cover.
 * A saved result is ?id=<quote id>&token=<claim token> (reloaded with GET /api/public/quote/{id}?token=).
 */
export const LOCODE_RE = /^[A-Z]{2}[A-Z2-9]{3}$/;

/** Next business day (Mon–Fri) in the browser's local time as "YYYY-MM-DD" — the same default the backend prices with. */
export function nextBusinessDay(from = new Date()) {
  const d = new Date(from.getFullYear(), from.getMonth(), from.getDate() + 1);
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() + 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function emptyForm() {
  return {
    origin: null,
    destination: null,
    mode: null,
    cargo: {
      load_type: "loose",
      packages: null,
      package_type: "pallet_eur",
      weight_kg: null,
      volume_m3: null,
      stackable: true,
      containers: [{ type: "40HC", count: 1 }],
      dangerous_goods: false,
    },
    ready_date: nextBusinessDay(),
    incoterm: "",
    incoterm_place: "",
    customs_clearance: true,
    insurance: "NONE",
  };
}

const numOrNull = (s) => {
  if (s === null || s === undefined || s === "") return null;
  const n = Number(String(s).replace(",", "."));
  return Number.isFinite(n) ? n : null;
};

function placeFromQuery(code, label) {
  if (!code) return null;
  if (LOCODE_RE.test(code)) return { unlocode: code, name: label || code, label: label || code, query: code };
  return { query: code, label: label || code };
}

function placeToQuery(p) {
  if (!p) return [null, null];
  if (p.unlocode) return [p.unlocode, p.name || p.label || p.unlocode];
  const q = p.query || p.label || p.name;
  return q ? [q, null] : [null, null];
}

export function hasRouteAndCargo(f) {
  return !!(f.origin && f.destination && cargoProblem(f.cargo) === null);
}

/** null when the cargo is enough to price, else an i18n key. */
export function cargoProblem(c) {
  if (!c) return "pub.w.need_cargo";
  if (c.load_type === "FCL") {
    return (c.containers || []).some((x) => x.type && x.count > 0) ? null : "pub.w.need_cargo";
  }
  return c.weight_kg > 0 ? null : "pub.w.need_cargo";
}

export function formFromParams(params) {
  const f = emptyForm();
  const g = (k) => params.get(k);
  f.origin = placeFromQuery(g("o"), g("ol"));
  f.destination = placeFromQuery(g("d"), g("dl"));
  const m = g("m");
  f.mode = ["sea", "road", "air", "rail"].includes(m) ? m : null;
  const lt = g("lt");
  if (lt === "FCL" || (!lt && g("c"))) f.cargo.load_type = "FCL";
  f.cargo.packages = numOrNull(g("n"));
  if (g("pt")) f.cargo.package_type = g("pt");
  f.cargo.weight_kg = numOrNull(g("w"));
  f.cargo.volume_m3 = numOrNull(g("v"));
  if (g("st") === "0") f.cargo.stackable = false;
  if (g("dg") === "1") f.cargo.dangerous_goods = true;
  if (g("c")) {
    const cs = g("c")
      .split(",")
      .map((x) => x.split(":"))
      .map(([type, count]) => ({ type, count: Math.max(1, parseInt(count, 10) || 1) }))
      .filter((x) => x.type);
    if (cs.length) f.cargo.containers = cs;
  }
  f.ready_date = /^\d{4}-\d{2}-\d{2}$/.test(g("r") || "") ? g("r") : nextBusinessDay();
  f.incoterm = g("i") || "";
  f.incoterm_place = g("ip") || "";
  if (g("cc") === "0") f.customs_clearance = false;
  if (g("ins")) f.insurance = g("ins");
  return f;
}

export function formToParams(f) {
  const p = new URLSearchParams();
  const [o, ol] = placeToQuery(f.origin);
  const [d, dl] = placeToQuery(f.destination);
  if (o) p.set("o", o);
  if (ol && ol !== o) p.set("ol", ol);
  if (d) p.set("d", d);
  if (dl && dl !== d) p.set("dl", dl);
  if (f.mode) p.set("m", f.mode);
  const c = f.cargo;
  if (c.load_type === "FCL") {
    p.set("lt", "FCL");
    const cs = (c.containers || []).filter((x) => x.type && x.count > 0).map((x) => `${x.type}:${x.count}`);
    if (cs.length) p.set("c", cs.join(","));
  } else {
    if (c.packages) p.set("n", String(c.packages));
    if (c.package_type && c.package_type !== "pallet_eur") p.set("pt", c.package_type);
    if (c.volume_m3) p.set("v", String(c.volume_m3));
    if (c.stackable === false) p.set("st", "0");
  }
  if (c.weight_kg) p.set("w", String(c.weight_kg));
  if (c.dangerous_goods) p.set("dg", "1");
  if (f.ready_date) p.set("r", f.ready_date);
  if (f.incoterm) p.set("i", f.incoterm);
  if (f.incoterm_place) p.set("ip", f.incoterm_place);
  if (f.customs_clearance === false) p.set("cc", "0");
  if (f.insurance && f.insurance !== "NONE") p.set("ins", f.insurance);
  return p;
}

function placeForRequest(p) {
  if (!p) return null;
  if ((p.unlocode && p.kinds) || (Number.isFinite(p.lat) && Number.isFinite(p.lon) && p.country)) return p; // full Location from the picker
  if (p.unlocode) return { query: p.unlocode };
  return { query: p.query || p.label || p.name };
}

/** Form state -> QuoteRequest body for POST /api/public/quote. */
export function formToRequest(f) {
  const c = f.cargo;
  const fcl = c.load_type === "FCL";
  return {
    mode: f.mode || null,
    origin: placeForRequest(f.origin),
    destination: placeForRequest(f.destination),
    ready_date: f.ready_date || null,
    incoterm: f.incoterm || null,
    incoterm_place: f.incoterm ? f.incoterm_place || null : null,
    customs_clearance: f.customs_clearance !== false,
    insurance: { cover: f.insurance || "NONE" },
    cargo: {
      load_type: fcl ? "FCL" : "loose",
      packages: fcl ? null : c.packages || null,
      package_type: fcl ? null : c.package_type || "pallet_eur",
      weight_kg: c.weight_kg || null,
      volume_m3: fcl ? null : c.volume_m3 || null,
      stackable: fcl ? null : c.stackable !== false,
      containers: fcl ? (c.containers || []).filter((x) => x.type && x.count > 0) : [],
      dangerous_goods: !!c.dangerous_goods,
    },
  };
}

/** Saved quote (quote.request) -> form state, so a reloaded result shows the inputs it was priced with. */
export function formFromQuote(quote) {
  const f = emptyForm();
  const r = quote?.request || {};
  f.origin = r.origin || null;
  f.destination = r.destination || null;
  f.mode = r.mode || null;
  const c = r.cargo || {};
  f.cargo = {
    ...f.cargo,
    load_type: c.load_type === "FCL" ? "FCL" : "loose",
    packages: c.packages ?? null,
    package_type: c.package_type || "pallet_eur",
    weight_kg: c.weight_kg ?? null,
    volume_m3: c.volume_m3 ?? null,
    stackable: c.stackable !== false,
    containers: Array.isArray(c.containers) && c.containers.length ? c.containers : f.cargo.containers,
    dangerous_goods: !!c.dangerous_goods,
  };
  f.ready_date = r.ready_date || nextBusinessDay();
  f.incoterm = r.incoterm || "";
  f.incoterm_place = r.incoterm_place || "";
  f.customs_clearance = r.customs_clearance !== false;
  f.insurance = r.insurance?.cover || "NONE";
  return f;
}
