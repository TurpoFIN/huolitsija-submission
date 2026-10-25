import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, Shuffle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, LoadingRows } from "@/components/app/states";
import { PageHeader } from "@/components/app/layout";
import { apiGet, apiPost, useApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { money, relTime } from "@/lib/format";
import { DemoLocale, LangToggle } from "./DemoLocale";
import "./demoflow.css";

const PHASE_BADGE = { running: "in-progress", offer_sent: "submitted", lifecycle: "in-progress", done: "success", failed: "failed", stopped: "expired",
  done_open: "expired" };
// Paid with a damage claim still with a person reads as complete but neutral (see DemoFlowPage).
const runStatus = (r) => (r.status === "done" && r.end_reason === "claim_review_handoff" ? "done_open"
  : r.phase === "lifecycle" && r.status === "running" ? "lifecycle" : r.status);
// The requester's facts: the requester sandbox answers the agent's questions from these, by e-mail.
const PROFILE = ["company", "contact_name", "cargo_description", "packages", "weight_kg", "volume_m3", "dimensions", "ready_date", "incoterm",
  "pickup_address", "delivery_address", "dangerous_goods"];

function Field({ id, label, value, onChange, wide, area }) {
  const Tag = area ? "textarea" : "input";
  return (
    <label className={wide ? "df-field df-field-wide" : "df-field"} htmlFor={id}>
      <span>{label}</span>
      <Tag id={id} className="surface-input df-input" value={value} rows={area ? 3 : undefined} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function StartRun({ t }) {
  const nav = useNavigate();
  const [f, setF] = useState({ origin: "", destination: "", text: "" });
  const [profile, setProfile] = useState({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const up = (k) => (v) => setF((s) => ({ ...s, [k]: v }));
  const random = async () => {
    setErr(null);
    try {
      const r = await apiGet("/demo/random-lane");
      setF((s) => ({ ...s, origin: r.origin, destination: r.destination }));
    } catch (e) {
      setErr(e);
    }
  };
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const run = await apiPost("/demo/runs", { ...f, profile });
      nav(`/ops/demo/${run.id}`);
    } catch (e2) {
      setErr(e2);
      setBusy(false);
    }
  };
  const ready = f.origin.trim().length > 1 && f.destination.trim().length > 1;
  return (
    <form className="df-start" onSubmit={submit}>
      <div className="df-start-lane">
        <Field id="df-origin" label={t("demoflow.start.from")} value={f.origin} onChange={up("origin")} />
        <ArrowRight className="df-start-arrow" aria-hidden />
        <Field id="df-dest" label={t("demoflow.start.to")} value={f.destination} onChange={up("destination")} />
        <Button type="button" variant="outline" onClick={random}><Shuffle className="size-4" aria-hidden />{t("demoflow.start.random")}</Button>
      </div>
      <Field id="df-text" label={t("demoflow.start.text")} value={f.text} onChange={up("text")} wide area />
      <details className="df-details">
        <summary>{t("demoflow.start.profile")}</summary>
        <div className="df-start-grid">
          {PROFILE.map((k) => (
            <Field key={k} id={`df-p-${k}`} label={t(`demoflow.profile.${k}`)} value={profile[k] || ""} onChange={(v) => setProfile((p) => ({ ...p, [k]: v }))} />
          ))}
        </div>
      </details>
      {err ? <ErrorState error={err} compact /> : null}
      <div><Button type="submit" variant="accent" disabled={!ready || busy}>{busy ? t("demoflow.start.starting") : t("demoflow.start.go")}</Button></div>
    </form>
  );
}

function DemoRunsInner() {
  const { t, locale } = useT();
  const q = useApi("/demo/runs", { pollMs: 10000 });
  const items = q.data?.items || [];
  return (
    <div className="mx-auto w-full max-w-[1152px]">
      <PageHeader title={t("demoflow.runs.title")} actions={<LangToggle />} />
      <StartRun t={t} />
      <h2 className="df-h2">{t("demoflow.runs.recent")}</h2>
      {q.loading ? <LoadingRows rows={4} /> : q.error ? <ErrorState error={q.error} onRetry={() => q.reload()} /> : items.length === 0 ? (
        <EmptyState title={t("demoflow.runs.empty")} />
      ) : (
        <div className="df-table-wrap">
          <table className="df-table df-runs">
            <thead><tr><th>{t("demoflow.runs.ref")}</th><th>{t("demoflow.runs.lane")}</th><th>{t("demoflow.runs.status")}</th><th className="df-r">{t("demoflow.runs.offer")}</th><th>{t("demoflow.runs.started")}</th></tr></thead>
            <tbody>
              {items.map((r) => (
                <tr key={r.id}>
                  <td><Link className="df-mono" to={`/ops/demo/${r.id}`}>{r.ref}</Link></td>
                  <td><Link to={`/ops/demo/${r.id}`}>{r.origin_text} → {r.destination_text}</Link></td>
                  <td><StatusBadge size="sm" state={PHASE_BADGE[runStatus(r)] || "in-progress"} label={t(`demoflow.status.${runStatus(r)}`)} /></td>
                  <td className="df-r df-num">{r.offer?.sell_cents ? money(r.offer.sell_cents, locale) : "—"}</td>
                  <td className="df-muted">{relTime(r.created_at, locale)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function DemoRuns() {
  return <DemoLocale><DemoRunsInner /></DemoLocale>;
}
