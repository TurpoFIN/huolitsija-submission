import React from "react";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { ApiMissing, Reasons, DecisionCard, bi, isMissingApi } from "./extCommon";

export function ControlResult({ data }) {
  const { t } = useT();
  return <div className="space-y-3">
    {[['you', data.you], ['bot', data.autopilot]].map(([key, c]) => c && <div key={key}><p className="mb-1 text-xs text-ink-secondary">{t(`cx.ctl.${key}`)}</p><StatusBadge size="sm" state={c.may_file === true ? "success" : "pending"} label={c.may_file == null ? t("cx.missing") : t(c.may_file ? "cx.ctl.allowed" : "cx.ctl.blocked")} /><Reasons className="mt-2" reasons={c.reasons} /></div>)}
    {data.four_eyes?.required && <p className="text-sm">{t("cx.ctl.four_eyes")}</p>}
    {data.eligible_approvers?.length > 0 && <p className="text-sm">{t("cx.ctl.approvers")}: {data.eligible_approvers.map(u => u.name || u.email).join(", ")}</p>}
    {data.approval && <DecisionCard decision={{ outcome: "owner_queue", reasons: data.approval.reasons, approval_id: data.approval.id, approval_status: data.approval.status }} />}
  </div>;
}
export default function ControlsDeclaration({ id, kind = "import" }) {
  const q = useApi(`/customs/${id}/controls?kind=${kind}`);
  if (isMissingApi(q.error)) return <ApiMissing />;
  return <>{q.error && <ErrorState compact error={q.error} onRetry={q.reload} />}{q.loading && !q.data && <LoadingRows rows={3} />}{q.data && <ControlResult data={q.data} />}</>;
}
export function ScreeningPanel({ decl, onScreen, busy }) {
  const { t, locale } = useT();
  const screening = decl.party_screening || decl.screening;
  const parties = screening?.parties || [];
  return <div className="space-y-3">
    {onScreen && <Button variant="outline" disabled={busy} onClick={onScreen}>{t(busy ? "state.saving" : "cx.screen.run")}</Button>}
    {!screening ? <p className="text-sm text-ink-secondary">{t("cx.screen.not_checked")}</p> : <>
      <p className="text-sm">{bi(locale, screening, "status_label") || t(`cx.screen.st.${screening.status}`)}</p>
      <Reasons reasons={screening.reasons} />
      {parties.map((p, i) => <div key={i} className="border-t pt-2 text-sm"><p>{p.name} · {bi(locale, p, "role_label") || p.role} · {bi(locale, p, "status_label") || t(`cx.screen.st.${p.status}`)}</p><Reasons reasons={p.reasons} />{(p.hits || []).map((h,j) => <p key={j} className="text-xs text-ink-secondary">{h.name} · {h.list} · {bi(locale, h, "explanation")}</p>)}</div>)}
    </>}
    {(decl.items || []).filter(it => it.controls?.dual_use).map(it => <div key={it.seq} className="border-t pt-2 text-sm"><p>#{it.seq} · {it.controls.dual_use.controlled == null ? t("cx.missing") : t(it.controls.dual_use.controlled ? "cx.screen.controlled" : "cx.screen.uncontrolled")}</p><Reasons reasons={it.controls.dual_use.reasons} />{(it.controls.dual_use.candidates || []).map(c => <details key={c.code || c.entry}><summary>{c.code || c.entry}</summary>{bi(locale,c,"text")}</details>)}</div>)}
  </div>;
}
