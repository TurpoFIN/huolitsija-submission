// EORI as the backend and the EU EOS check take it (backend services/customs.py eori_shape_ok): Finnish = FI + Business ID
// with or without the hyphen (FI1234567-8); other countries = country code + 1-15 letters or digits.
export function eoriShapeOk(v) {
  const s = String(v || "").replace(/\s+/g, "").toUpperCase();
  if (s.startsWith("FI")) return /^FI\d{7}-?\d$/.test(s);
  return /^[A-Z]{2}[A-Z0-9]{1,15}$/.test(s);
}

/** "shipper" / "consignee" / "notify" when the API rejected that party's EORI (routes/quotes.py _check_party), else null. */
export function eoriErrorParty(detail) {
  const m = /^The (shipper|consignee|notify party)'s EORI number looks wrong/.exec(typeof detail === "string" ? detail : "");
  return m ? (m[1] === "notify party" ? "notify" : m[1]) : null;
}
