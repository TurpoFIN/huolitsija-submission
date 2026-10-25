// E-mail threads of one shipment or quote, embedded on its ops page. Each thread renders compactly with its
// decision bar, mails and reply box. section: wrap in a titled Section and render nothing until a thread exists.
import React from "react";
import { Section } from "@/components/app/layout";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { EmailConversation } from "./EmailThreadView";

export default function EmailThreadPanel({ shipmentId, quoteId, section = false }) {
  const { t } = useT();
  const path = shipmentId ? `/email-channel/for?shipment_id=${encodeURIComponent(shipmentId)}`
    : quoteId ? `/email-channel/for?quote_id=${encodeURIComponent(quoteId)}` : null;
  const q = useApi(path, { pollMs: 30000 });
  const items = q.data?.items || [];

  if (section) {
    // Quote page: the section exists only when there is e-mail to show (no permission / failure stays silent until data).
    if (!items.length) return null;
    return (
      <Section title={t("email.section")}>
        {q.error && <ErrorState compact className="mb-2" error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
        <Threads items={items} reload={q.reload} />
      </Section>
    );
  }

  if (q.loading && !q.data) return <LoadingRows rows={3} />;
  if (q.error && !q.data) return <ErrorState error={q.error} onRetry={q.reload} />;
  if (!items.length) {
    return <p className="py-2 text-sm text-ink-tertiary">{shipmentId ? t("email.panel.empty_shipment") : t("email.panel.empty_quote")}</p>;
  }
  return (
    <>
      {q.error && <ErrorState compact className="mb-2" error={{ message: t("state.failed_refresh") }} onRetry={q.reload} />}
      <Threads items={items} reload={q.reload} />
    </>
  );
}

function Threads({ items, reload }) {
  return (
    <div className="flex flex-col gap-4">
      {items.map((th) => (
        <div key={th.id} className="overflow-hidden surface-flat rounded-xl">
          <EmailConversation data={th} reload={reload} compact />
        </div>
      ))}
    </div>
  );
}
