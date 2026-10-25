// quotes dictionary (flat dotted keys). Split into parts so portal, ops and shared quote UI can be edited independently.
import shared from "../features/quotes/i18n/shared";
import portal from "../features/quotes/i18n/portal";
import ops from "../features/quotes/i18n/ops";

const PARTS = [shared, portal, ops];
const en = {};
const fi = {};
PARTS.forEach((p) => {
  Object.assign(en, p.en || {});
  Object.assign(fi, p.fi || {});
});
export default { en, fi };
