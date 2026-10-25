import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nProvider } from '@/lib/i18n';
import QuoteOptions from './QuoteOptions';
import SourcingStatus from './SourcingStatus';
import { displayText } from '@/lib/localizedData';

const render = child => renderToStaticMarkup(<I18nProvider>{child}</I18nProvider>);

test('estimate cannot be booked and its carrier is honestly labelled', () => {
  const html = render(<QuoteOptions options={[{ id: 'estimate', mode: 'air', carrier_name: 'Market estimate',
    service: 'AIR', total_cents: 42000, firmness: 'estimate', bookable: false, legs: [], tags: [],
    door_to_door_days: 5, transit_days: 5 }]} onSelect={() => {}} />);
  expect(html).toContain('Markkina-arvio');
  expect(html).toContain('Hinta-arvio');
  expect(html).toContain('Odottaa vahvistettua hintaa');
  expect(html).toMatch(/disabled=""[^>]*data-select/);
});

test('sourcing shows actual Finnish status and distinguishes a default response estimate', () => {
  const html = render(<SourcingStatus quote={{ sourcing: { status: 'requested', eta_at: '2026-10-25T12:00:00Z',
    eta_basis: 'default reply time for the mode (no partner history yet)',
    message_fi: 'Pyydämme kumppaneilta hinnan. Arvio 25.10.2026 klo 14.00.' } }} />);
  expect(html).toContain('25.10.2026 klo 14.00');
  expect(html).toContain('Aika ei ole sitova');
  expect(html).not.toContain('default reply');
});

test.each([
  ["40HC, index all-in: base freight incl. BAF/CAF/PSS, feeder port", 'syöttösatama'],
  ['100 kg chargeable (1:6000), small-shipment break × 1.2, index all-in incl. fuel/security', 'pienlähetyskerroin'],
  ['Air via Nairobi – Dubai', 'Lentorahti reitillä'],
  ['Derived from FAX China–North Europe (weekly) (2026-10-20) scaled by distance 6 182 km and trade direction', 'etäisyys 6 182 km'],
  ['Typical terminal handling by region', 'Alueen tyypillinen'],
])('price provenance is translated: %s', (source, expected) => {
  expect(displayText(source, 'fi')).toContain(expected);
  expect(displayText(source, 'en')).toBe(source);
});

test('an indicative market rate is labelled as such, not as a carrier rate', () => {
  const opt = { id: 'ind', mode: 'road', carrier_name: 'PostNord', service: 'ROAD_LTL', total_cents: 49008, firmness: 'firm',
    bookable: true, rate_source: 'indicative', price_status: 'indicative', legs: [], tags: [], door_to_door_days: 2, transit_days: 2 };
  const html = render(<QuoteOptions options={[opt]} onSelect={() => {}} />);
  expect(html).toContain('Ohjeellinen hinta – vahvistetaan varauksen yhteydessä');
  expect(html).not.toContain('Hinnastohinta');
  const older = render(<QuoteOptions options={[{ ...opt, price_status: undefined }]} onSelect={() => {}} />);
  expect(older).toContain('Ohjeellinen hinta');  // quotes priced before the label existed
  const contract = render(<QuoteOptions options={[{ ...opt, price_status: undefined, price_basis: 'contract' }]} onSelect={() => {}} />);
  expect(contract).toContain('Hinnastohinta');
});
