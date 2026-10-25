import { localizePayload } from '@/lib/localizedData';

test('published surcharge payload keeps provider identifiers and translates Finnish display text', () => {
  const payload = {
    items: [
      { id: 'bluedart-caf', label: 'Blue Dart CAF' },
      { id: 'swissport-hel-import-thc', label: 'Swissport HEL import THC' },
      { id: 'swissport-hel-export-thc', label: 'Swissport HEL export THC' },
      { id: 'maersk-westmed-na-pss', label: 'Maersk West Mediterranean → US/Canada' },
      { id: 'arkas-wrs-sajed', label: 'Arkas EWRS SAJED / JOAQJ' },
    ],
    sources: [{ state: 'unavailable', reason: 'Provider tariff could not be verified (ConnectError)' }],
    not_quoted: [{ code: 'public_tariff.unavailable', reason: 'Required public surcharge could not be calculated' }],
    warnings: [{ message: 'A carrier option was excluded because a required public surcharge is unavailable.' }],
  };
  const fi = localizePayload(payload, 'fi');
  expect(fi.items.map(x => x.label)).toEqual([
    'Blue Dart CAF', 'Swissport HEL tuonnin terminaalikäsittely (THC)',
    'Swissport HEL viennin terminaalikäsittely (THC)',
    'Maersk Länsi-Välimeri → Yhdysvallat/Kanada', 'Arkas EWRS SAJED / JOAQJ',
  ]);
  expect(fi.items.map(x => x.id)).toEqual(payload.items.map(x => x.id));
  expect(fi.sources[0].reason).toBe('Kuljetusliikkeen tariffia ei voitu vahvistaa.');
  expect(fi.not_quoted[0].reason).toBe('Pakollista julkista lisämaksua ei voitu laskea.');
  expect(fi.warnings[0].message).toBe('Kuljetusvaihtoehto jätettiin pois, koska pakollista julkista lisämaksua ei ole saatavilla.');
  expect(localizePayload(payload, 'en')).toEqual(payload);
});
