import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Section } from "@/components/app/layout";
import { apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";

export default function InvoiceDispute({ inv, internal = false, onChanged }) {
  const { t, locale } = useT();
  const { can } = useAuth();
  const [current, setCurrent] = useState(null);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [amount, setAmount] = useState("");
  const [credit, setCredit] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => { setCurrent(null); }, [inv.dispute]);
  const d = current || inv.dispute;
  const open = d && ["submitted", "in_review"].includes(d.status);
  const writable = internal ? can("billing.write") : can("company.book") || can("company.manage");
  const money = (cents) => new Intl.NumberFormat(locale === "fi" ? "fi-FI" : "en-GB", {
    style: "currency", currency: inv.currency || "EUR",
  }).format(cents / 100);
  if (inv.kind !== "invoice" || !inv.number || inv.status === "draft") return null;
  const credits = (inv.related || []).filter((c) => c.kind === "credit_note" && c.status !== "draft" && c.status !== "void");
  const validText = text.trim().length >= 10;
  const cents = Math.round(Number(amount.replace(",", ".")) * 100);
  const validAmount = /^\d+([.,]\d{1,2})?$/.test(amount.trim()) && Number.isFinite(cents) && cents > 0 && cents <= inv.total_cents;
  async function submit(action) {
    setBusy(true); setError(""); setSaved(false);
    try {
      let url = `/invoices/${inv.id}/dispute`;
      let body = { reason: text.trim(), amount_cents: cents };
      if (action === "message") { url += `/${d.id}/messages`; body = { text: text.trim() }; }
      else if (action !== "new") { url += `/${d.id}/resolve`; body = { outcome: action, note: text.trim(), credit_note_id: credit || null }; }
      const result = await apiPost(url, body);
      setCurrent(result); setText(""); setEditing(false); setSaved(true); onChanged?.();
    } catch (e) { setError(e.message || t("dispute.failed")); }
    finally { setBusy(false); }
  }
  return <Section title={t("dispute.title")}>
    {saved && <p role="status" className="mb-2 text-sm">{t("dispute.saved")}</p>}
    {error && <p role="alert" className="mb-2 text-sm text-destructive">{error}</p>}
    {d ? <div className="space-y-2 text-sm">
      <p><span className="font-medium">{t(`dispute.status.${d.status}`)}</span> · {money(d.amount_cents)}</p>
      {open && <p className="text-ink-secondary">{t("dispute.hold")}</p>}
      <ol className="space-y-3">
        {d.messages.map((m) => <li key={m.id}>
          <p className="text-xs text-ink-secondary">{m.by} · {new Date(m.at).toLocaleString(locale === "fi" ? "fi-FI" : "en-GB")}</p>
          <p className="whitespace-pre-wrap break-words">{m.text}</p>
        </li>)}
      </ol>
      {d.resolution?.credit_note_id && <Link className="text-ink-accent hover:underline"
        to={`${internal ? "/ops/billing" : "/portal/invoices"}/${d.resolution.credit_note_id}`}>{d.resolution.credit_note_number}</Link>}
    </div> : <p className="text-sm text-ink-secondary">{t("dispute.empty")}</p>}
    {writable && !open && !editing && !["void", "credited"].includes(inv.status) &&
      <Button size="sm" variant="outline" className="mt-3" onClick={() => { setEditing(true); setSaved(false); }}>{t("dispute.new")}</Button>}
    {writable && (open || editing) && <div className="mt-3 max-w-xl space-y-3">
      {editing && <label className="block text-sm">{t("dispute.amount", { currency: inv.currency || "EUR" })}
        <Input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" autoComplete="off" />
        <span className="text-xs text-ink-secondary">{t("dispute.maximum", { amount: money(inv.total_cents) })}</span>
      </label>}
      <label className="block text-sm">{t(editing ? "dispute.reason" : "dispute.reply")}
        <Textarea value={text} onChange={(e) => setText(e.target.value)} minLength={10} maxLength={4000} rows={3} />
        <span className="text-xs text-ink-secondary">{t("dispute.length")}</span>
      </label>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" disabled={busy || !validText || (editing && !validAmount)} onClick={() => submit(editing ? "new" : "message")}>
          {t(busy ? "dispute.saving" : editing ? "dispute.submit" : "dispute.send")}
        </Button>
        {editing && <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(false)}>{t("action.cancel")}</Button>}
        {open && !internal && <Button size="sm" variant="outline" disabled={busy || !validText} onClick={() => submit("withdrawn")}>{t("dispute.withdraw")}</Button>}
      </div>
      {open && internal && <div className="border-t border-border pt-3 space-y-2">
        <p className="text-xs text-ink-secondary">{t("dispute.resolve_help")}</p>
        <label className="block text-sm">{t("dispute.credit")}
          <select className="w-full rounded-md border border-input bg-background p-2 text-sm" value={credit} onChange={(e) => setCredit(e.target.value)}>
            <option value="">{t("dispute.choose_credit")}</option>
            {credits.map((c) => <option key={c.id} value={c.id}>{c.number} · {money(c.total_cents)}</option>)}
          </select>
        </label>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={busy || !validText || !credit} onClick={() => submit("credited")}>{t("dispute.resolve_credit")}</Button>
          <Button size="sm" variant="outline" disabled={busy || !validText} onClick={() => submit("rejected")}>{t("dispute.reject")}</Button>
        </div>
      </div>}
    </div>}
    {!!inv.dispute_history?.length && <details className="mt-3 text-sm"><summary>{t("dispute.history")}</summary>
      {inv.dispute_history.map((h) => <div key={h.id} className="mt-2"><p>{t(`dispute.status.${h.status}`)} · {money(h.amount_cents)}</p><p className="whitespace-pre-wrap">{h.reason}</p><p>{h.resolution?.note}</p></div>)}
    </details>}
  </Section>;
}
