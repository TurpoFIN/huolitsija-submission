// Pure helpers for the compliance UI (no React, so jest can import them without the router).

/** Raw server detail of an ApiError: FastAPI's {"detail": "..."} or validation list, else null. */
export function rawDetail(error) {
  const d = error?.data?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ");
  return null;
}

/** Translated label for a key family, or the raw value when the catalogue has no entry (never blank). */
export function label(t, prefix, value) {
  if (value === null || value === undefined || value === "") return "—";
  const k = `${prefix}.${value}`;
  const s = t(k);
  return s === k ? String(value).replace(/_/g, " ") : s;
}

/** Policy rows from {items:[...]}, {policy:[...]} or a {key: {...}} map; period in years or days. */
export function policyRows(d) {
  const src = Array.isArray(d) ? d : d?.items || d?.policy || d?.rules || null;
  if (Array.isArray(src)) return src;
  const obj = d?.policy && typeof d.policy === "object" ? d.policy : d && typeof d === "object" ? d : {};
  return Object.entries(obj).filter(([, v]) => v && typeof v === "object" && !Array.isArray(v)).map(([k, v]) => ({ key: k, ...v }));
}

