export const domainErrors = [
  ["Enter a value / Syötä arvo", "Syötä arvo."],
  ["Enter a number / Syötä numero", "Syötä kelvollinen luku."],
  ["Syötä summa kahdella desimaalilla / Enter an amount with up to two decimals", "Syötä summa enintään kahdella desimaalilla."],
  ["Type enable to enable autonomous actions", "Vahvista käyttöönotto kirjoittamalla enable."],
  ["Margin must be at least 0 and below 100 percent", "Katteen on oltava vähintään 0 ja alle 100 prosenttia."],
  ["Price limit must be a positive whole number of cents", "Hinnan on oltava positiivinen summa kokonaisina sentteinä."],
  ["Lanes use mode:ORIGIN>DESTINATION, for example road:FI>SE", "Reitin muoto on kuljetusmuoto:ALKUMAA>LOPPUMAA, esimerkiksi road:FI>SE."],
  ["Enter a valid Finnish IBAN", "Syötä kelvollinen suomalainen IBAN."],
  ["Enter a valid BIC", "Syötä kelvollinen BIC."],
  ["Enter a valid Finnish business ID", "Syötä kelvollinen Y-tunnus."],
  ["Enter a Finnish VAT ID, FI followed by eight digits", "Syötä ALV-tunnus: FI ja kahdeksan numeroa."],
  ["MRN must match the accepted declaration", "MRN:n on vastattava hyväksyttyä tulli-ilmoitusta."],
  ["Enter the customs decision date", "Syötä tullauspäätöksen päivämäärä."],
  ["The decision date cannot be in the future", "Päätöspäivä ei voi olla tulevaisuudessa."],
  ["Enter decided duty and import VAT for every goods item exactly once", "Syötä päätetty tulli ja tuonnin ALV jokaiselle tavaraerälle kerran."],
  ["Decision amounts must be explicit non-negative whole cents", "Syötä jokainen päätöksen summa kokonaisina sentteinä, vähintään nolla."],
  ["TARIC additional codes must each contain four letters or digits", "Jokaisessa TARIC-lisäkoodissa on oltava neljä kirjainta tai numeroa."],
  ["Explain the TARIC evidence clearing the anti-dumping risk (at least 10 characters)", "Perustele TARIC-tiedon pohjalta, miksi polkumyyntitullia ei sovelleta (vähintään 10 merkkiä)."],
  ["Partner Network is off", "Kumppaniverkosto ei ole käytössä."],
  ["Offer purchase price must be positive whole cents", "Syötä tarjouksen ostohinta positiivisina kokonaisina sentteinä."],
  ["Enter the carrier offer's expiry date", "Syötä kuljetusliikkeen tarjouksen viimeinen voimassaolopäivä."],
  ["Offer must be current and include transit days (1–180)", "Tarjouksen on oltava voimassa. Syötä kuljetusaika väliltä 1–180 päivää."],
  ["A rate card is already recorded for this RFQ", "Tälle tarjouspyynnölle on jo kirjattu hinnasto."],
  ["This action cannot be reversed", "Tätä toimea ei voi kumota."],
  ["Accepted or booked quotes need a cancellation agreement", "Hyväksytty tai varattu tarjous vaatii erillisen peruutussopimuksen."],
];

export function domainError(error, t) {
  const text=error?.message || String(error);
  const index=domainErrors.findIndex(([en])=>en===text);
  return index>=0?t(`domain.error.${index}`):text;
}
