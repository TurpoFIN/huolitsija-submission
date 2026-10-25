import React from "react";
import { Section } from "@/components/app/layout";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { ApiMissing, isMissingApi } from "./extCommon";

export function BtiRulings({ data }) {
  const { t } = useT();
  return <>
    <p className="mb-2 text-xs text-ink-secondary"><span className="font-mono">CN {data.code}</span> · {t("cx.bti.note")}</p>
    {!data.rulings.length ? <p className="text-sm">{t("cx.bti.none")}</p> : <ul className="divide-y divide-border">{data.rulings.map(r => <li key={r.ref} className="py-3 text-sm"><a href={r.url} target="_blank" rel="noreferrer" className="font-mono text-ink-accent hover:underline">{r.ref}</a><span className="ml-2 text-xs text-ink-secondary">{r.country} · {r.valid_from} – {r.valid_to}</span><p className="mt-1">{r.description}</p>{r.justification && <details className="mt-1 text-xs"><summary className="cursor-pointer">{t("cx.taric.col.legal")}</summary><p className="whitespace-pre-wrap">{r.justification}</p></details>}</li>)}</ul>}
  </>;
}
export default function TaricBti({ code }) {
  const cn = String(code || "").replace(/\D/g, "").slice(0, 8);
  const q = useApi(`/customs/bti?code=${encodeURIComponent(cn)}`, { enabled: !!code });
  const status = useApi("/customs/bti/status", { enabled: !!code });
  const { t } = useT();
  if (!code) return null;
  return <Section title={t("cx.bti.title")}>
    {isMissingApi(q.error, { any404: true }) ? <ApiMissing /> : <>
      {q.error && <ErrorState compact error={q.error} onRetry={q.reload} />}
      {q.loading && !q.data && <LoadingRows rows={2} />}
      {status.error && <ErrorState compact error={status.error} onRetry={status.reload} />}
      {q.data && (status.data?.full_at || status.data?.daily_published || q.data.rulings.length > 0 ? <BtiRulings data={q.data} /> : <p className="text-sm text-ink-secondary">{t("cx.bti.unindexed")}</p>)}
    </>}
  </Section>;
}
