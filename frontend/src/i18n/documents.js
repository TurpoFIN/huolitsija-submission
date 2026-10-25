// V3 dictionary: documents, customs, company / KYC. Each feature keeps its strings next to its code
// (src/features/<area>/i18n.js) and this file merges them for the app-wide i18n index.
import documents from "@/features/documents/i18n";
import customs from "@/features/customs/i18n";
import company from "@/features/company/i18n";

const en = { ...documents.en, ...customs.en, ...company.en };
const fi = { ...documents.fi, ...customs.fi, ...company.fi };
export default { en, fi };
