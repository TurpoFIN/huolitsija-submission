import { displayText } from '@/lib/localizedData';
import cases from './ferryBookingText.cases.json';

test.each(cases)('ferry booking text: $en', ({ en, fi }) => {
  expect(displayText(en, 'fi')).toBe(fi);
  expect(displayText(en, 'en')).toBe(en);
});
