import React, { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { apiGet, apiPost } from "@/lib/api";
import { homeFor, useAuth } from "@/lib/auth";
import { tEnum, useT } from "@/lib/i18n";
import { AuthLayout, Field } from "./AuthLayout";

export default function AcceptInvite() {
  const { t } = useT();
  const { token } = useParams();
  const { refresh } = useAuth();
  const navigate = useNavigate();
  const [info, setInfo] = useState(null);
  const [loadErr, setLoadErr] = useState(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => { apiGet(`/auth/invite/${token}`).then(setInfo).catch(setLoadErr); }, [token]);
  const ok = name && password.length >= 10 && /[A-Za-z]/.test(password) && /\d/.test(password);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiPost("/auth/accept-invite", { token, name, password });
      const r = await refresh();
      navigate(homeFor(r?.user), { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  if (loadErr) return <AuthLayout title={t("auth.invite.invalid")}><ErrorState error={loadErr} /></AuthLayout>;
  if (!info) return <AuthLayout title="…"><LoadingRows rows={3} /></AuthLayout>;
  return (
    <AuthLayout title={t("auth.invite.title", { company: info.company_name })}>
      <p className="mb-4 text-sm text-ink-secondary">{t("auth.invite.body", { inviter: info.invited_by || "Huolitsija", role: tEnum(t, "role", info.role) })}</p>
      <form onSubmit={submit}>
        <Field label={t("field.email")} htmlFor="em"><Input id="em" value={info.email} disabled /></Field>
        <Field label={t("field.name")} htmlFor="nm"><Input id="nm" value={name} onChange={(e) => setName(e.target.value)} autoFocus /></Field>
        <Field label={t("field.password")} htmlFor="pw" hint={t("auth.signup.password_hint")}>
          <Input id="pw" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        {error && <ErrorState className="mb-3" compact error={error} />}
        <Button type="submit" className="w-full" disabled={!ok || busy}>{t("auth.invite.submit")}</Button>
      </form>
    </AuthLayout>
  );
}
