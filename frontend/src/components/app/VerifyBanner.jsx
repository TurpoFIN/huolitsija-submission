import React, { useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";

/** Slim banners for account state: unverified e-mail, company not yet verified. */
export function AccountBanners() {
  const { user, company } = useAuth();
  const { t } = useT();
  const [sent, setSent] = useState(false);
  if (!user || user.org_type !== "customer") return null;
  return (
    <div className="mb-4 flex flex-col gap-2">
      {!user.email_verified && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg px-3 py-2 text-sm status-in-progress">
          <span className="flex-1 min-w-0">{t("verify.banner", { email: user.email })}</span>
          <Button size="xs" variant="outline" disabled={sent} onClick={() => apiPost("/auth/resend-verification", {}).then(() => setSent(true))}>
            {sent ? t("verify.sent") : t("verify.resend")}
          </Button>
        </div>
      )}
      {company && company.kyc_status !== "approved" && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg px-3 py-2 text-sm status-pending">
          <span className="flex-1 min-w-0">{t("overview.kyc_needed")}</span>
          <Button size="xs" variant="outline" asChild><Link to="/portal/company">{t("overview.kyc_cta")}</Link></Button>
        </div>
      )}
    </div>
  );
}
