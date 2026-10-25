// Customer self-service privacy (GDPR art. 15, 17, 20): download my data, delete my account. Both ask for the password.
// Erasure says up front what the law makes us keep (invoices 6 years, customs records) and then lists exactly what the
// API reports as erased and as retained, so the customer sees the real outcome rather than a promise.
import React, { useEffect, useState } from "react";
import { Download, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { apiPost, fileUrl } from "@/lib/api";
import { date, dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { ApiFail, label } from "./common";

function Row({ title, hint, children }) {
  return (
    <div className="flex flex-col gap-3 border-b border-[color:var(--border-subtle)] py-3.5 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
      <div className="min-w-0 max-w-prose"><p className="text-sm font-medium">{title}</p>{hint && <p className="mt-0.5 text-xs text-ink-tertiary">{hint}</p>}</div>
      <div className="flex shrink-0 flex-col gap-2 sm:w-80">{children}</div>
    </div>
  );
}

/** A download link for the export: server URL (relative /api/… or absolute) or a JSON bundle turned into a file. */
function useExportHref(res) {
  const [blobUrl, setBlobUrl] = useState(null);
  useEffect(() => {
    if (!res || res.download_url) return undefined;
    const url = URL.createObjectURL(new Blob([JSON.stringify(res, null, 2)], { type: "application/json" }));
    setBlobUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [res]);
  if (!res) return null;
  if (res.download_url) return /^\/api\//.test(res.download_url) ? fileUrl(res.download_url.slice(4)) : res.download_url;
  return blobUrl;
}

function Export() {
  const { t, locale } = useT();
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const href = useExportHref(res);
  const run = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    setRes(null);
    try { setRes(await apiPost("/privacy/me/export?format=json", { password: pw })); setPw(""); } catch (ex) { setErr(ex); } finally { setBusy(false); }
  };
  return (
    <Row title={t("privacy.export.title")} hint={t("privacy.export.hint")}>
      <form onSubmit={run} className="flex flex-col gap-2">
        <Input type="password" autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} placeholder={t("privacy.password")} aria-label={t("privacy.password")} />
        <Button type="submit" variant="outline" disabled={busy || !pw}><Download /> {busy ? t("state.loading") : t("privacy.export.run")}</Button>
      </form>
      {err && <ApiFail compact error={err} />}
      {res && href && (
        <div className="flex flex-col gap-1 rounded-lg bg-secondary px-3 py-2 text-sm" aria-live="polite">
          <span>{t("privacy.export.ready")}</span>
          <a href={href} download={res.download_url ? undefined : `huolitsija-tietoni-${new Date().toISOString().slice(0, 10)}.json`} className="font-medium text-ink-accent underline underline-offset-4">{t("privacy.export.download")}</a>
          {res.expires_at && <span className="text-xs text-ink-tertiary">{t("privacy.export.expires", { time: dateTime(res.expires_at, locale) })}</span>}
        </div>
      )}
    </Row>
  );
}

/** One kept / erased item: a string, or {label|category|collection, reason|basis, until|kept_until, count}. */
function Item({ it }) {
  const { locale } = useT();
  if (typeof it === "string") return <li>{it}</li>;
  const name = it.label || it.category || it.collection || it.what || it.key;
  const why = it.reason || it.basis || it.legal_basis;
  const until = it.until || it.kept_until || it.retain_until;
  return (
    <li>
      <span>{name}{typeof it.count === "number" ? ` (${it.count})` : ""}</span>
      {(why || until) && <span className="text-ink-tertiary"> · {[why, until && date(until, locale)].filter(Boolean).join(" · ")}</span>}
    </li>
  );
}

const asList = (v) => (Array.isArray(v) ? v : v && typeof v === "object" ? Object.entries(v).map(([k, x]) => (typeof x === "object" ? { key: k, ...x } : { key: k, count: x })) : []);

function Erase() {
  const { t } = useT();
  const word = t("privacy.erase.confirm_word");
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const run = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try { setRes(await apiPost("/privacy/me/erasure", { password: pw, confirm: "POISTA" })); setPw(""); } catch (ex) { setErr(ex); } finally { setBusy(false); }
  };
  const retained = asList(res?.retained);
  const erased = asList(res?.erased);
  return (
    <Row title={t("privacy.erase.title")} hint={t("privacy.erase.hint")}>
      {res ? (
        <div className="flex flex-col gap-2 text-sm" aria-live="polite">
          <p className="flex flex-wrap items-center gap-2">
            <StatusBadge size="sm" state={res.status === "completed" || !res.status ? "success" : res.status === "held" ? "pending" : "in-progress"} label={res.status ? label(t, "compliance.priv.status", res.status) : t("privacy.erase.done")} />
          </p>
          <p>{t("privacy.erase.done")}</p>
          {(res.held || res.legal_hold) && <p className="text-warning-fg">{t("privacy.erase.held")}</p>}
          {erased.length > 0 && <div><p className="text-xs font-medium text-ink-tertiary">{t("privacy.erase.erased")}</p><ul className="list-disc pl-5 text-xs">{erased.map((it, i) => <Item key={i} it={it} />)}</ul></div>}
          {retained.length > 0 && <div><p className="text-xs font-medium text-ink-tertiary">{t("privacy.erase.retained")}</p><ul className="list-disc pl-5 text-xs">{retained.map((it, i) => <Item key={i} it={it} />)}</ul></div>}
          <Button variant="outline" size="sm" asChild><a href="/">{t("privacy.erase.signed_out")}</a></Button>
        </div>
      ) : !open ? (
        <Button variant="outline" className="text-danger-fg" onClick={() => setOpen(true)}><Trash2 /> {t("privacy.erase.start")}</Button>
      ) : (
        <form onSubmit={run} className="flex flex-col gap-2">
          <Input type="password" autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} placeholder={t("privacy.password")} aria-label={t("privacy.password")} autoFocus />
          <label className="text-xs text-ink-secondary">{t("privacy.erase.confirm_label")}
            <Input className="mt-1 font-mono" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" />
          </label>
          {err && <ApiFail compact error={err} />}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="destructive" disabled={busy || !pw || confirm.trim().toUpperCase() !== word}>{busy ? t("state.loading") : t("privacy.erase.run")}</Button>
            <Button type="button" variant="ghost" onClick={() => { setOpen(false); setPw(""); setConfirm(""); setErr(null); }}>{t("privacy.erase.cancel")}</Button>
          </div>
        </form>
      )}
    </Row>
  );
}

export default function PrivacyPanel() {
  const { t } = useT();
  return (
    <Section title={t("privacy.panel.title")}>
      <p className="mb-1 max-w-prose text-sm text-ink-secondary">{t("privacy.panel.lead")}</p>
      <Export />
      <Erase />
    </Section>
  );
}
