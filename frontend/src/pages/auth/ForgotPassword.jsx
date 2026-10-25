import React, { useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorState } from "@/components/app/states";
import { StatusBadge } from "@/components/app/StatusBadge";
import { apiPost } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { AuthLayout, Field } from "./AuthLayout";

export default function ForgotPassword() {
  const { t } = useT();
  const [email, setEmail] = useState("");
  const [state, setState] = useState("idle");
  const [error, setError] = useState(null);
  const submit = async (e) => {
    e.preventDefault();
    setState("busy");
    setError(null);
    try {
      await apiPost("/auth/forgot-password", { email });
      setState("sent");
    } catch (err) {
      setError(err);
      setState("idle");
    }
  };
  return (
    <AuthLayout title={t("auth.forgot.title")}>
      {state === "sent" ? (
        <StatusBadge state="success" label={t("auth.forgot.sent")} className="h-auto whitespace-normal py-1" />
      ) : (
        <form onSubmit={submit}>
          <p className="mb-4 text-sm text-ink-secondary">{t("auth.forgot.body")}</p>
          <Field label={t("field.email")} htmlFor="email">
            <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
          </Field>
          {error && <ErrorState className="mb-3" compact error={error} />}
          <Button type="submit" className="w-full" disabled={!email || state === "busy"}>{t("auth.forgot.submit")}</Button>
        </form>
      )}
      <p className="mt-4 text-sm"><Link to="/login" className="text-ink-secondary hover:text-foreground">{t("action.back")}</Link></p>
    </AuthLayout>
  );
}
