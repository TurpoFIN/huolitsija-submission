// V4 dictionary: billing, messages, exceptions, claims. Each feature keeps its strings next to its code
// (src/features/<area>/strings.js, keys prefixed with the area) and this file merges them for i18n/index.js.
import billing from "@/features/billing/strings";
import messages from "@/features/messages/strings";
import exceptions from "@/features/exceptions/strings";
import claims from "@/features/claims/strings";

const parts = [billing, messages, exceptions, claims];
const en = Object.assign({}, ...parts.map((p) => p.en || {}));
const fi = Object.assign({}, ...parts.map((p) => p.fi || {}));
export default { en, fi };
