// Status history from decl.history[], with the remaining standard steps shown as future (grey).
import React from "react";
import { Timeline } from "@/components/app/Timeline";
import { tEnum, useT } from "@/lib/i18n";

const PATH = ["draft", "ready_for_review", "approved", "submitted", "accepted", "released"];
const TONE = { rejected: "danger", not_released: "danger", invalidated: "danger", under_control: "warning" };

export default function StatusTimeline({ decl, showActor = true }) {
  const { t } = useT();
  const history = decl?.history || [];
  const events = history.map((h, i) => ({
    id: `${h.at}-${i}`,
    at: h.at,
    title: tEnum(t, "customs", h.to),
    detail: h.note,
    source: showActor ? h.by?.name : null,
    tone: TONE[h.to],
  }));
  const v = decl?.approval_voided;
  if (v?.at) {
    events.push({ id: `voided-${v.at}`, at: v.at, title: t("customs.tl.approval_voided"), detail: v.reason,
      source: showActor ? v.by?.name : null, tone: "warning" });
    events.sort((a, b) => new Date(a.at) - new Date(b.at));
  }
  const idx = PATH.indexOf(decl?.status);
  if (idx >= 0) {
    PATH.slice(idx + 1).forEach((s) => events.push({ id: `f-${s}`, title: tEnum(t, "customs", s), future: true }));
  }
  return <Timeline events={events} />;
}
