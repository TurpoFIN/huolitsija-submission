import { deliveryState } from './text';
test('failed and log-only attempts never become delivery success', () => {
  expect(deliveryState([])).toBe('notSent');
  expect(deliveryState([{to:'a',status:'logged'}])).toBe('logged');
  expect(deliveryState([{to:'a',status:'sent'},{to:'b',status:'failed'}])).toBe('failed');
  expect(deliveryState([{to:'a',status:'failed'},{to:'a',status:'sent'}])).toBe('sent');
});
