// Public data-subject request on the privacy notice: type + e-mail, then the one-time code from the e-mail.
// No account needed; the code proves control of the address before anything is exported or erased.
import React, { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Segmented } from "@/components/app/FilterBar";
import { apiPost } from "@/lib/api";
import { date } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { ApiFail } from "./common";

export default function PrivacyRequestForm() {
  const { t, locale } = useT();
  const [type, setType] = useState("export");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [req, setReq] = useState(null);
  const [done, setDone] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const send = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try { setReq(await apiPost("/privacy/requests", { type, email: email.trim(), locale })); } catch (ex) { setErr(ex); } finally { setBusy(false); }
  };
  const verify = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try { setDone(await apiPost(`/privacy/requests/${req.id}/verify`, { code: code.trim() })); } catch (ex) { setErr(ex); } finally { setBusy(false); }
  };
  const reset = () => { setReq(null); setDone(null); setCode(""); setErr(null); };
  return (
    <section aria-labelledby="privacy-request" className="mt-12 rounded-xl border border-[color:var(--border-subtle)] p-4 sm:p-5">
      <h2 id="privacy-request" className="mb-1 text-lg font-medium leading-7">{t("privacy.form.title")}</h2>
      <p className="mb-4 text-sm text-ink-secondary">{t("privacy.form.lead")}</p>
      {done ? (
        <div className="flex flex-col items-start gap-3" aria-live="polite">
          <p className="text-sm">{done.due_at || req?.due_at ? t("privacy.form.verified", { date: date(done.due_at || req.due_at, locale) }) : t("privacy.form.verified_nodate")}</p>
          <Button variant="outline" size="sm" onClick={reset}>{t("privacy.form.again")}</Button>
        </div>
      ) : req ? (
        <form onSubmit={verify} className="flex flex-col gap-3" aria-live="polite">
          <p className="text-sm">{t("privacy.form.sent", { email: req.email_masked || email.trim() })}</p>
          <label className="text-sm">{t("privacy.form.code")}
            <Input className="mt-1 max-w-[220px] font-mono tracking-widest" value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" autoFocus />
          </label>
          {err && <ApiFail compact error={err} />}
          <div className="flex gap-2">
            <Button type="submit" disabled={busy || !code.trim()}>{busy ? t("state.loading") : t("privacy.form.verify")}</Button>
            <Button type="button" variant="ghost" onClick={reset}>{t("privacy.erase.cancel")}</Button>
          </div>
        </form>
      ) : (
        <form onSubmit={send} className="flex flex-col gap-3">
          <div>
            <p className="mb-1 text-sm">{t("privacy.form.type")}</p>
            <Segmented value={type} onChange={setType} label={t("privacy.form.type")}
              options={["export", "erasure"].map((k) => ({ value: k, label: t(`compliance.priv.type.${k}`) }))} />
          </div>
          <label className="text-sm">{t("privacy.form.email")}
            <Input type="email" required autoComplete="email" className="mt-1 max-w-[360px]" value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          {err && <ApiFail compact error={err} />}
          <div><Button type="submit" disabled={busy || !/^\S+@\S+\.\S+$/.test(email.trim())}>{busy ? t("state.loading") : t("privacy.form.send")}</Button></div>
        </form>
      )}
    </section>
  );
}
