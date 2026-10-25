/** Package kinds for a goods item, each one the customs filing maps to a UN/ECE Rec. 21 code (backend
 *  services/tulli.py PACKAGE_CODES). Stored values come from documents as written ("cartons", "CTNS"), so they are
 *  matched to a kind for display and kept as-is unless the user picks another kind. */
export const PACKAGE_KINDS = ["carton", "box", "pallet", "case", "crate", "bag", "sack", "drum", "roll", "coil", "bundle", "bale", "container", "package"];
const ALIASES = { ctn: "carton", ctns: "carton", plt: "pallet", pcs: "package", pieces: "package", pallet_eur: "pallet", pallet_fin: "pallet" };

export function packageKind(value) {
  const s = String(value ?? "").trim().toLowerCase();
  if (!s) return null;
  if (PACKAGE_KINDS.includes(s)) return s;
  if (ALIASES[s]) return ALIASES[s];
  if (s.endsWith("es") && PACKAGE_KINDS.includes(s.slice(0, -2))) return s.slice(0, -2);
  if (s.endsWith("s") && PACKAGE_KINDS.includes(s.slice(0, -1))) return s.slice(0, -1);
  return null;
}

/** Select options [{value, kind}] for the current value: its kind's option keeps the stored value (so opening the
 *  editor changes nothing), and a value that matches no kind is offered as written. */
export function packageOptions(current) {
  const cur = String(current ?? "").trim();
  const kind = packageKind(cur);
  const opts = PACKAGE_KINDS.map((k) => ({ value: k === kind ? cur : k, kind: k }));
  if (cur && !kind) opts.push({ value: cur, kind: null });
  return opts;
}
