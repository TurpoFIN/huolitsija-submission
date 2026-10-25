// E-mail channel display helpers: backend enums -> i18n labels and workflow visual states.
// Labels live in src/i18n/email.js (prefix "email."). Unknown values never show as raw keys.
import { tEnum } from "@/lib/i18n";

export const INTENTS = ["quote_request", "book", "booking_details", "documents", "question", "change_request", "cancel",
  "complaint", "payment", "thanks", "unsubscribe", "other"];

/** Thread state -> one of the nine workflow visuals (StatusBadge state). */
export const THREAD_STATE = {
  new: "in-progress",
  awaiting_info: "pending",
  quote_review: "in-review",
  quoted: "submitted",
  booking_details: "pending",
  booking_pending_kyc: "pending",
  booked: "scheduled",
  invoiced: "scheduled",
  paid: "success",
  owner: "flagged",
};

/** Filter groups for the inbox pills (client-side over the loaded list). */
export const STATE_GROUPS = {
  quote: ["new", "awaiting_info", "quote_review", "quoted"],
  booking: ["booking_details", "booking_pending_kyc"],
  shipment: ["booked"],
  invoice: ["invoiced", "paid"],
};
export const FILTERS = ["owner", "all", "quote", "booking", "shipment", "invoice"];

export function stateLabel(t, state) {
  const key = `email.state.${state || "new"}`;
  const s = t(key);
  return s === key ? t("email.state.new") : s;
}

export function intentLabel(t, intent) {
  if (!intent) return null;
  const key = `email.intent.${intent}`;
  const s = t(key);
  return s === key ? t("email.intent.other") : s;
}

/** Owner-queue reason ("ambiguous", "intent:complaint", ...) -> sentence. */
export function reasonText(t, reason) {
  if (!reason) return t("email.reason.other");
  if (reason.startsWith("intent:")) {
    const intent = intentLabel(t, reason.slice(7));
    return intent ? `${t("email.reason.intent")} · ${intent}` : t("email.reason.intent");
  }
  const key = `email.reason.${reason}`;
  const s = t(key);
  return s === key ? t("email.reason.other") : s;
}

const REASONS = new Set(["possible_prompt_injection", "ambiguous", "sender_differs", "model_unavailable",
  "booking_without_thread_headers", "quote_failed", "processing_failed", "channel_off", "sender_authentication_failed",
  "booking_without_quote", "documents_without_attachments", "attachments_not_accepted", "documents_without_shipment",
  "question_without_customer"]);

/** Machine decision string -> short human text, or null when there is nothing worth showing. */
export function decisionText(t, decision) {
  if (!decision) return null;
  if (REASONS.has(decision) || decision.startsWith("intent:")) return reasonText(t, decision);
  const [head, ...rest] = decision.split(":");
  const tail = rest.join(":");
  switch (head) {
    case "quote": {
      const [number, status] = rest;
      return t("email.decision.quote", { number: number || "—", status: tEnum(t, "quote", status) });
    }
    case "booked":
      return t("email.decision.booked", { ref: tail || "—" });
    case "documents_filed":
      return t("email.decision.documents_filed", { n: Number(tail) || 0 });
    case "automatic":
    case "no_reply":
    case "asked_for_missing":
    case "asked_for_booking_details":
    case "booking_failed":
    case "posted_to_messages":
    case "own_mail":
    case "kyc_link_sent":
    case "already_booked":
    case "estimate_not_bookable":
    case "documents_kept_for_booking":
      return t(`email.decision.${head}`);
    default:
      return null;
  }
}

/** Inbound processing status -> {state, label}; "processed" is the quiet default (no badge). */
export const IN_STATUS = {
  received: "in-progress", processing: "in-progress", processed: null, needs_owner: "pending", ignored: "expired",
  failed: "failed", retry: "pending", handled_by_owner: "success", dismissed: "expired",
};
/** Outbound delivery status -> visual; "sent" stays quiet text. */
export const OUT_STATUS = { sent: null, sent_via_app: null, logged: "expired", failed: "failed", sending: "in-progress" };

export function tagLabel(t, tag) {
  if (!tag) return null;
  const key = `email.tag.${tag}`;
  const s = t(key);
  if (s !== key) return s;
  if (tag.startsWith("email_kyc_")) return t("email.tag.email_kyc_link");
  return null;
}

export function matchLabel(t, match) {
  if (!match) return null;
  const key = `email.match.${match}`;
  const s = t(key);
  return s === key ? null : s;
}

/** Collapse whitespace for one-line previews. */
export const oneLine = (s) => (s || "").replace(/\s+/g, " ").trim();

/** Messages that need a person: waiting for a decision, or failed processing. */
export const needsDecision = (m) => m.direction === "in" && (m.status === "needs_owner" || m.status === "failed");

export const emailPath = (threadId, messageId) =>
  `/ops/email${threadId ? `/${threadId}` : ""}${messageId ? `?message=${encodeURIComponent(messageId)}` : ""}`;
