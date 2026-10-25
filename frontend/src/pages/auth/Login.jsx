import React, { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorState } from "@/components/app/states";
import { homeFor, useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { AuthLayout, Field } from "./AuthLayout";

export default function Login() {
  const { t } = useT();
  const { login } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await login(email, password);
      const next = params.get("next");
      navigate(next && next.startsWith("/") ? next : homeFor(r?.user), { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthLayout title={t("auth.login.title")}>
      <form onSubmit={submit} noValidate>
        <Field label={t("field.email")} htmlFor="email">
          <Input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
        </Field>
        <Field label={t("field.password")} htmlFor="password">
          <Input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </Field>
        {error && <ErrorState className="mb-3" compact error={error} />}
        <Button type="submit" className="w-full" disabled={busy}>{busy ? t("state.loading") : t("auth.login.submit")}</Button>
        <div className="mt-4 flex items-center justify-between text-sm">
          <Link to="/forgot-password" className="text-ink-secondary hover:text-foreground">{t("auth.login.forgot")}</Link>
          <Link to={`/signup${params.get("claim") ? `?claim=${params.get("claim")}` : ""}`} className="text-ink-accent hover:underline">{t("auth.login.create")}</Link>
        </div>
      </form>
    </AuthLayout>
  );
}
