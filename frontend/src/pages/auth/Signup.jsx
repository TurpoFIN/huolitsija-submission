import React, { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { ErrorState } from "@/components/app/states";
import { apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { AuthLayout, Field } from "./AuthLayout";

export default function Signup() {
  const { t, locale } = useT();
  const { refresh } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [form, setForm] = useState({ name: "", email: "", company_name: "", password: "", accept_terms: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [tried, setTried] = useState(false);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e?.target ? e.target.value : e }));
  const pwOk = form.password.length >= 10 && /[A-Za-z]/.test(form.password) && /\d/.test(form.password);

  const invalid = {
    name: !form.name.trim(),
    company_name: !form.company_name.trim(),
    email: !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(form.email),
    password: !pwOk,
    accept_terms: !form.accept_terms,
  };
  const err = (k, msg) => (tried && invalid[k] ? msg : null);

  const submit = async (e) => {
    e.preventDefault();
    setTried(true);
    if (Object.values(invalid).some(Boolean)) return;
    setBusy(true);
    setError(null);
    try {
      await apiPost("/auth/signup", { ...form, locale });
      const claim = params.get("claim");
      let quoteId = null;
      if (claim) {
        const r = await apiPost("/quotes/claim", { token: claim }).catch(() => null);
        quoteId = r?.id || r?.quote?.id || null;
      }
      await refresh();
      navigate(quoteId ? `/portal/quotes/${quoteId}` : "/portal/company?welcome=1", { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const aside = (
    <div>
      <h2 className="text-2xl leading-9 mb-6">{t("auth.signup.side_title")}</h2>
      <ul className="flex flex-col gap-3 text-sm text-ink-secondary">
        {["auth.signup.side_1", "auth.signup.side_2", "auth.signup.side_3"].map((k) => (
          <li key={k} className="flex gap-2"><Check className="mt-0.5 size-4 shrink-0 text-ink-tertiary" aria-hidden />{t(k)}</li>
        ))}
      </ul>
    </div>
  );

  return (
    <AuthLayout title={t("auth.signup.title")} aside={aside}>
      <form onSubmit={submit} noValidate>
        <Field label={t("field.name")} htmlFor="name" error={err("name", t("form.required"))}>
          <Input id="name" autoComplete="name" value={form.name} onChange={set("name")} required autoFocus />
        </Field>
        <Field label={t("field.company")} htmlFor="company" hint={t("auth.signup.company_hint")} error={err("company_name", t("form.required"))}>
          <Input id="company" autoComplete="organization" value={form.company_name} onChange={set("company_name")} required />
        </Field>
        <Field label={t("field.email")} htmlFor="email" error={err("email", t("form.invalid_email"))}>
          <Input id="email" type="email" autoComplete="email" value={form.email} onChange={set("email")} required />
        </Field>
        <Field label={t("field.password")} htmlFor="password" hint={t("auth.signup.password_hint")} error={err("password", t("auth.signup.password_hint"))}>
          <Input id="password" type="password" autoComplete="new-password" value={form.password} onChange={set("password")} required
            aria-invalid={tried && !pwOk} />
        </Field>
        <label className="mb-4 mt-1 flex items-start gap-2 text-sm text-ink-secondary">
          <Checkbox checked={form.accept_terms} onCheckedChange={(v) => setForm((f) => ({ ...f, accept_terms: !!v }))} className="mt-0.5" aria-label={t("auth.signup.accept")} />
          <span>
            {t("auth.signup.accept")} · <Link to="/terms" target="_blank" className="underline">NSAB</Link> · <Link to="/privacy" target="_blank" className="underline">GDPR</Link>
          </span>
        </label>
        {tried && invalid.accept_terms && <p className="-mt-2 mb-3 text-xs text-danger-fg" role="alert">{t("form.accept_terms")}</p>}
        {error && <ErrorState className="mb-3" compact error={error} />}
        <Button type="submit" className="w-full" disabled={busy}>
          {busy ? t("state.saving") : t("auth.signup.submit")}
        </Button>
        <p className="mt-4 text-sm text-ink-secondary">
          {t("auth.signup.have_account")} <Link to="/login" className="text-ink-accent hover:underline">{t("auth.login.submit")}</Link>
        </p>
      </form>
    </AuthLayout>
  );
}
