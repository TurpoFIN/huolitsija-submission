import React, { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorState } from "@/components/app/states";
import { apiPost } from "@/lib/api";
import { homeFor, useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { AuthLayout, Field } from "./AuthLayout";

export default function ResetPassword() {
  const { t } = useT();
  const { refresh } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ok = password.length >= 10 && /[A-Za-z]/.test(password) && /\d/.test(password);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiPost("/auth/reset-password", { token: params.get("token"), password });
      const r = await refresh();
      navigate(homeFor(r?.user), { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <AuthLayout title={t("auth.reset.title")}>
      <form onSubmit={submit}>
        <Field label={t("settings.new_password")} htmlFor="pw" hint={t("auth.signup.password_hint")}>
          <Input id="pw" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
        </Field>
        {error && <ErrorState className="mb-3" compact error={error} />}
        <Button type="submit" className="w-full" disabled={!ok || busy}>{t("auth.reset.submit")}</Button>
      </form>
    </AuthLayout>
  );
}
