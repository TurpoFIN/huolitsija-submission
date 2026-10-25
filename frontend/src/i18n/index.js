// Merges every area's dictionary. Each slice owns one file; add new files to AREAS.
import finance from "./finance";
import disputes from "./disputes";
import recourse from "./recourse";
import domain from "./domain";
import common from "./common";
import publicSite from "./public";
import portal from "./portal";
import ops from "./ops";
import shipments from "./shipments";
import quotes from "./quotes";
import documents from "./documents";
import billing from "./billing";
import uiReview from "./uiReview";
import plurals from "./plurals";
import compliance from "./compliance";
import sourcing from "./sourcing";
import channels from "./channels";
import insights from "./insights";
import s14api from "./s14api";
import edi from "./edi";
import autopilot from "./autopilot";
import customsExt from "./customsExt";
import commercial from "./commercial";
import procurement from "./procurement";

import execution from "./execution";
import email from "./email";
import globalQuote from "./globalQuote";
import runbook from "./runbook";
import demoflow from "./demoflow";

const AREAS = [disputes, recourse, execution, globalQuote, email, finance, domain, common, publicSite, portal, ops, shipments, quotes, documents, billing, uiReview, plurals, sourcing, channels, insights, s14api, edi, autopilot, commercial, compliance, customsExt, procurement, runbook, demoflow];
const out = { en: {}, fi: {} };
AREAS.forEach((a) => {
  Object.assign(out.en, a.en || {});
  Object.assign(out.fi, a.fi || {});
});
export default out;
