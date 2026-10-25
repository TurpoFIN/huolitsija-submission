import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/app/StatusBadge";
import { apiPost } from "@/lib/api";
import { homeFor, useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { AuthLayout } from "./AuthLayout";

export default function VerifyEmail() {
  const { t } = useT();
  const { user, refresh } = useAuth();
  const [params] = useSearchParams();
  const [state, setState] = useState("pending");
  useEffect(() => {
    const token = params.get("token");
    if (!token) { setState("failed"); return; }
    apiPost("/auth/verify-email", { token }).then(() => { setState("ok"); refresh(); }).catch(() => setState("failed"));
  }, [params, refresh]);
  return (
    <AuthLayout title={t("auth.verify.title")}>
      {state === "pending" && <StatusBadge state="in-progress" label={t("state.loading")} />}
      {state === "ok" && <StatusBadge state="success" label={t("auth.verify.ok")} />}
      {state === "failed" && <StatusBadge state="failed" label={t("auth.verify.fail")} />}
      <div className="mt-5">
        <Button asChild><Link to={user ? homeFor(user) : "/login"}>{t("auth.verify.continue")}</Link></Button>
      </div>
    </AuthLayout>
  );
}
