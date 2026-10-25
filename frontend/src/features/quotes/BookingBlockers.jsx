/**
 * <BookingBlockers items company /> — account-side reasons a quote cannot be booked yet, translated by code, each with its link.
 *   items    booking_blockers[] from GET /api/quotes/{id} or POST /accept: {code "kyc"|"sanctions"|"company"|"status"|"expired", message, link}
 *   className
 *   company  quote.company {kyc_status, sanctions_status} (optional, adds the verification state)
 * "status" and "expired" are left out: the pages show quote status and validity themselves. Unknown codes show the backend message.
 */
import React from "react";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export const ACCOUNT_BLOCKERS = ["kyc", "sanctions", "company"];

export default function BookingBlockers({ items, company, className }) {
  const { t } = useT();
  const list = (items || []).filter((b) => !["status", "expired"].includes(b.code));
  if (list.length === 0) return null;
  const text = (b) => {
    if (b.code === "kyc") return t("pq.blocker.kyc", { state: tEnum(t, "kyc", company?.kyc_status || "not_started") });
    if (b.code === "sanctions") return t("pq.blocker.sanctions");
    if (b.code === "company") return t("pq.blocker.company");
    return b.message;
  };
  return (
    <ul className={cn("flex flex-col gap-1.5", className)} role="alert">
      {list.map((b, i) => (
        <li key={`${b.code}-${i}`} className="rounded-lg px-2.5 py-1.5 text-sm status-pending">
          <span>{text(b)}</span>
          {b.link && (
            <Link to={b.link} className="ml-1.5 inline-flex items-center gap-0.5 font-medium underline underline-offset-2">
              {t(`pq.blocker.link.${b.code}`) === `pq.blocker.link.${b.code}` ? t("pq.blocker.open") : t(`pq.blocker.link.${b.code}`)}
              <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          )}
        </li>
      ))}
    </ul>
  );
}

