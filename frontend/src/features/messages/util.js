// Shared helpers for the messages feature (portal, ops, shipment panel).
import { useCallback, useState } from "react";
import { apiUpload } from "@/lib/api";
import { date } from "@/lib/format";

export const MAX_BODY = 8000;
// Same set the documents API accepts (PDF, PNG, JPG, WEBP, CSV, XLSX; max 20 MB).
export const ACCEPT = ".pdf,.png,.jpg,.jpeg,.webp,.csv,.xlsx";

/** Plural-aware translation: key_one / key_other, with {n} available. */
export function tn(t, key, n, vars = {}) {
  return t(`${key}_${n === 1 ? "one" : "other"}`, { n, ...vars });
}

// A background AI draft takes seconds; a "drafting" flag much older than the customer's message is stale
// (e.g. the server restarted mid-draft), so the UI offers the button again instead of an endless spinner.
const DRAFTING_STALE_MS = 3 * 60 * 1000;
export const draftingFresh = (iso) => !!iso && Date.now() - new Date(iso).getTime() < DRAFTING_STALE_MS;

export const inboxPath = (mode) => (mode === "ops" ? "/ops/messages" : "/portal/messages");
export const shipmentPath = (mode, id) => (mode === "ops" ? `/ops/shipments/${id}` : `/portal/shipments/${id}`);

/** Which side of the conversation a message sits on, from the viewer's point of view. */
export function isOwnSide(mode, authorType) {
  if (mode === "ops") return authorType === "ops" || authorType === "ai";
  return authorType === "customer";
}

const loc = (l) => (l === "fi" ? "fi-FI" : "en-GB");

/** Mail-client style time for list rows: today -> 9.06, yesterday -> Eilen, this week -> ma, else 24.10. */
export function compactTime(iso, locale, t) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const ts = d.getTime();
  if (ts >= startToday) return d.toLocaleTimeString(loc(locale), { hour: "numeric", minute: "2-digit" });
  if (ts >= startToday - 86400000) return t("messages.time.yesterday");
  if (ts >= startToday - 6 * 86400000) return d.toLocaleDateString(loc(locale), { weekday: "short" });
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString(loc(locale), { day: "numeric", month: "numeric" });
  return date(iso, locale);
}

/** Message timestamp: "24.10. klo 9.06" style, full date for other years. */
export function messageTime(iso, locale) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleString(loc(locale), {
    day: "numeric", month: "numeric", ...(sameYear ? {} : { year: "numeric" }), hour: "numeric", minute: "2-digit",
  });
}

// System reasons the backend writes on superseded/rejected drafts -> i18n keys (a person's own note is shown as written).
const SYSTEM_REASONS = {
  "Replaced by a newer AI draft": "messages.reason.newer_ai_draft",
  "Replaced by a newer draft": "messages.reason.newer_draft",
  "A person replied": "messages.reason.person_replied",
  "Rejected by a person": "messages.reason.rejected_by_person",
};
export function reasonText(reason, t) {
  if (!reason) return null;
  return SYSTEM_REASONS[reason] ? t(SYSTEM_REASONS[reason]) : reason;
}

export function fileSize(bytes, locale) {
  if (bytes === null || bytes === undefined) return null;
  const nf = (v, d) => new Intl.NumberFormat(loc(locale), { maximumFractionDigits: d }).format(v);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${nf(bytes / 1024, 0)} ${locale === "fi" ? "kt" : "KB"}`;
  return `${nf(bytes / (1024 * 1024), 1)} ${locale === "fi" ? "Mt" : "MB"}`;
}

export function pct(v, locale) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return `${new Intl.NumberFormat(loc(locale), { maximumFractionDigits: 0 }).format(Number(v) * 100)} %`;
}

/**
 * Pending attachments for a composer. Files upload when the message is sent; an upload that already
 * succeeded keeps its document id, so a retry after a failed send does not upload twice.
 * target: { shipment_id?, company_id? } (company_id is needed by ops for threads without a shipment).
 */
export function useAttachments() {
  const [files, setFiles] = useState([]); // [{key, file, docId, error}]
  const [progress, setProgress] = useState(null); // {i, n} while uploading
  const add = useCallback((list) => {
    const items = Array.from(list || []).map((file) => ({ key: `${file.name}-${file.size}-${Math.random().toString(36).slice(2)}`, file, docId: null }));
    if (items.length) setFiles((f) => [...f, ...items]);
  }, []);
  const remove = useCallback((key) => setFiles((f) => f.filter((x) => x.key !== key)), []);
  const clear = useCallback(() => setFiles([]), []);
  const uploadAll = useCallback(async (target = {}) => {
    const ids = [];
    const todo = files;
    for (let i = 0; i < todo.length; i += 1) {
      const item = todo[i];
      if (item.docId) { ids.push(item.docId); continue; }
      setProgress({ i: i + 1, n: todo.length });
      const fd = new FormData();
      fd.append("file", item.file);
      fd.append("type", "other");
      if (target.shipment_id) fd.append("shipment_id", target.shipment_id);
      else if (target.company_id) fd.append("company_id", target.company_id);
      try {
        const doc = await apiUpload("/documents", fd);
        ids.push(doc.id);
        setFiles((f) => f.map((x) => (x.key === item.key ? { ...x, docId: doc.id } : x)));
      } finally {
        setProgress(null);
      }
    }
    return ids;
  }, [files]);
  return { files, add, remove, clear, uploadAll, progress };
}

/** Per-thread unsent composer text, kept in memory while the app is open (switching threads keeps drafts). */
const drafts = new Map();
export const composerDraft = {
  get: (key) => drafts.get(key) || "",
  set: (key, v) => (v ? drafts.set(key, v) : drafts.delete(key)),
};
