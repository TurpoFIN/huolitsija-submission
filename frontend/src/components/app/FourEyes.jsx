// Why the requester cannot approve their own customs filing or claim payment, and (single ops user) the audited override.
import React from "react";
import { Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { OVERRIDE_MIN_NOTE, overrideReady } from "@/lib/fourEyes";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function FourEyesNotice({ state, note, onOverride, busy, className }) {
  const { t } = useT();
  if (!state) return null;
  const single = state === "single_user";
  return (
    <div role="status" className={cn("flex flex-col gap-2 rounded-lg px-3 py-2 text-sm status-pending", className)}>
      <p className="flex items-start gap-2">
        <Users className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span>
          <span className="font-medium">{t("approvals.four_eyes.title")}</span>{" "}
          {single ? t("approvals.four_eyes.single_user", { n: OVERRIDE_MIN_NOTE }) : t("approvals.four_eyes.colleague")}
        </span>
      </p>
      {single && onOverride && (
        <div className="flex flex-wrap items-center gap-2 pl-6">
          <Button size="sm" variant="outline" onClick={onOverride} disabled={busy || !overrideReady(note)}>
            {t("approvals.four_eyes.override")}
          </Button>
          <span className="text-xs">{overrideReady(note) ? t("approvals.four_eyes.audited") : t("approvals.four_eyes.reason_needed", { n: OVERRIDE_MIN_NOTE })}</span>
        </div>
      )}
    </div>
  );
}
