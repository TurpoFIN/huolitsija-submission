// Compact invoice list for one shipment (shipment detail, billing tab). Ops: "Create invoice" for a delivered
// shipment that has no invoice yet (POST /invoices/from-shipment/{id}), then onChange().
import React, { useState } from "react";
import { Link } from "react-router-dom";
import { Receipt } from "lucide-react";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Loadable, LoadingRows } from "@/components/app/states";
import { apiPost, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { ActionNote } from "./InvoiceActions";
import { invoiceLink } from "./InvoiceDocument";
import { dueInfo, eur, isoDate } from "./invoiceFormat";

export default function ShipmentInvoicesPanel({ shipment, mode = "customer", onChange }) {
  const { t, locale } = useT();
  const { can } = useAuth() || {};
  const id = shipment?.id;
  const q = useApi(id ? `/invoices?shipment_id=${id}` : null, { enabled: !!id });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [note, setNote] = useState(null);
  if (!id) return null;

  const items = q.data?.items || [];
  const hasInvoice = items.some((i) => i.kind === "invoice" && i.status !== "void");
  const ops = mode === "ops";
  const delivered = shipment.status === "delivered";
  const canWrite = ops && can?.("billing.write");

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const inv = await apiPost(`/invoices/from-shipment/${id}`, {});
      setNote({ text: inv.status === "draft" ? t("billing.panel.created_draft") : t("billing.panel.created_issued", { n: inv.number || "" }),
        link: { to: invoiceLink("ops", inv.id), label: t("action.open") } });
      q.reload();
      onChange?.(inv);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const createButton = ops && delivered && !hasInvoice ? (
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" onClick={create} disabled={busy || !canWrite} title={!canWrite ? t("billing.act.no_permission") : undefined}>
        {busy ? t("billing.panel.creating") : t("billing.panel.create")}
      </Button>
      {!canWrite && <span className="text-xs text-ink-tertiary">{t("billing.act.no_permission")}</span>}
    </div>
  ) : null;

  return (
    <div className="flex flex-col gap-2">
      <ActionNote note={note} onClose={() => setNote(null)} />
      {error && <ErrorState error={error} compact />}
      <Loadable q={q} skeleton={<LoadingRows rows={2} />} isEmpty={(d) => (d.items || []).length === 0}
        empty={
          <EmptyState icon={Receipt} className="py-5"
            title={t("billing.panel.empty")}
            body={ops ? (delivered ? t("billing.panel.empty_ops_delivered") : t("billing.panel.empty_ops_not_delivered")) : t("billing.panel.empty_customer")}
            action={createButton} />
        }>
        {(d) => (
          <>
            <ul className="flex flex-col divide-y divide-[color:var(--border-subtle)]">
              {d.items.map((inv) => {
                const due = dueInfo(t, inv);
                return (
                  <li key={inv.id}>
                    <Link to={invoiceLink(mode, inv.id)} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-1 py-2 hover:bg-hover">
                      <span className="font-mono text-[13px]">{inv.number || t("billing.draft")}</span>
                      {inv.kind === "credit_note" && <span className="text-xs text-ink-tertiary">{t("billing.kind.credit_note")}</span>}
                      <StatusBadge group="invoice" size="sm" value={inv.status} />
                      <span className="ml-auto text-sm tabular">{eur(inv.total_cents, locale)}</span>
                      <span className={cn("w-full text-xs tabular sm:w-auto", due?.tone === "warning" ? "text-warning-fg" : "text-ink-tertiary")}>
                        {inv.due_date ? `${t("billing.col.due")} ${isoDate(inv.due_date, locale)}` : "—"}{due ? ` · ${due.text}` : ""}
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
            {createButton}
          </>
        )}
      </Loadable>
    </div>
  );
}
