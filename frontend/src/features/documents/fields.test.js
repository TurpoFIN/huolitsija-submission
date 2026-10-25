import dictionaries from "@/i18n";
import { reviewReason } from "./fields";

const t = (key, vars = {}) => (dictionaries.fi[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => vars[k]);

test("the review reason names missing fields once, as sentences", () => {
  const ex = {
    status: "needs_review",
    reason: "Tämä näyttää olevan pakkausluettelo, ei kauppalasku. Vaihda tyyppi, jos se pitää paikkansa.; Ei löytynyt asiakirjasta; Ei löytynyt asiakirjasta",
    warnings: [
      { code: "type_mismatch", severity: "error", message: "Tämä näyttää olevan pakkausluettelo, ei kauppalasku. Vaihda tyyppi, jos se pitää paikkansa." },
      { code: "missing", severity: "error", field: "invoice_number", message: "Ei löytynyt asiakirjasta" },
      { code: "missing", severity: "error", field: "invoice_date", message: "Ei löytynyt asiakirjasta" },
      { code: "model_note", severity: "info", message: "Huomio" },
    ],
  };
  expect(reviewReason(ex, t)).toBe(
    "Tämä näyttää olevan pakkausluettelo, ei kauppalasku. Vaihda tyyppi, jos se pitää paikkansa. "
    + "Ei löytynyt asiakirjasta: Laskun numero, Laskun päiväys.");
});

test("without blocking warnings the stored reason is shown", () => {
  expect(reviewReason({ status: "needs_review", reason: "Jotkin arvot olivat vaikealukuisia", warnings: [] }, t)).toBe("Jotkin arvot olivat vaikealukuisia");
  expect(reviewReason({ status: "failed", reason: "Lukeminen epäonnistui", warnings: [{ severity: "error", message: "x" }] }, t)).toBe("Lukeminen epäonnistui");
});
