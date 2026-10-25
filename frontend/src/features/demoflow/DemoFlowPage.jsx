import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, Banknote, Maximize2, Minimize2, PanelRight, Pause, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import FlowMap, { useNodeLabel } from "./FlowMap";
import NodePanel, { clockTime } from "./NodePanel";
import { RUN_STATUS_BADGE, isRunEvent, summary } from "./model";
import { useTexts } from "./titles";
import { RunAccess, useRunStream } from "./useRunStream";
import { DemoLocale, LangToggle } from "./DemoLocale";
import "./demoflow.css";

// Simulated seconds per real second. 1440 = one day per minute (the lifecycle default).
export const SPEEDS = [
  { v: 1, key: "demoflow.speed.live" },
  { v: 60, key: "demoflow.speed.hour" },
  { v: 1440, key: "demoflow.speed.day" },
  { v: 4320, key: "demoflow.speed.days3" },
];
const PHASE_BADGE = RUN_STATUS_BADGE;

function simNow(clock, fallback) {
  if (!clock?.anchor_sim) return fallback ? new Date(fallback) : null;
  const base = new Date(clock.anchor_sim).getTime();
  if (clock.paused) return new Date(base);
  return new Date(base + (Date.now() - new Date(clock.anchor_real).getTime()) * Number(clock.speed || 1));
}

function SimClock({ run, lastSimAt, stoppedAt, ended, onSet, locale, t, readOnly = false }) {
  const [, force] = useState(0);
  const clock = run?.clock;
  const running = run && !ended && !["done", "failed", "stopped"].includes(run.status);
  useEffect(() => {
    if (!running || clock?.paused) return undefined;
    const iv = setInterval(() => force((n) => n + 1), clock && Number(clock.speed) > 1 ? 100 : 1000);
    return () => clearInterval(iv);
  }, [running, clock]);
  // A stopped run's clock holds the sim time of the stop.
  const frozenAt = ended ? stoppedAt || lastSimAt : lastSimAt;
  const now = running ? simNow(clock, lastSimAt) : frozenAt ? new Date(frozenAt) : null;
  const speed = Number(clock?.speed || 1);
  const [busy, setBusy] = useState(false);
  const set = async (patch) => {
    setBusy(true);
    try { await onSet(patch); } finally { setBusy(false); }
  };
  const loc = locale === "fi" ? "fi-FI" : "en-GB";
  return (
    <div className="df-clock" role="group" aria-label={t("demoflow.clock")}>
      <div className="df-clock-read">
        <span className="df-clock-date">
          {ended ? `${t("demoflow.clockStopped")} · ` : ""}{now ? now.toLocaleDateString(loc, { weekday: "short", day: "numeric", month: "short" }) : "—"}
        </span>
        <span className="df-clock-time df-num">{now ? now.toLocaleTimeString(loc, { hour: "2-digit", minute: "2-digit", second: speed < 60 ? "2-digit" : undefined }) : "—"}</span>
      </div>
      {readOnly ? null : <>
      <button type="button" className="df-iconbtn" disabled={!running || busy} onClick={() => set({ paused: !clock?.paused })}
        aria-label={clock?.paused ? t("demoflow.resume") : t("demoflow.pause")}>
        {clock?.paused ? <Play /> : <Pause />}
      </button>
      {/* Phones: the same choice as a select, so no option is cut off at the edge. */}
      <select className="df-speed-select surface-input" aria-label={t("demoflow.speed")} disabled={!running || busy}
        value={String(SPEEDS.find((s) => Math.abs(speed - s.v) < 0.5)?.v ?? "")} onChange={(e) => set({ speed: Number(e.target.value) })}>
        {SPEEDS.find((s) => Math.abs(speed - s.v) < 0.5) ? null : <option value="">{t("demoflow.speed")}</option>}
        {SPEEDS.map((s) => <option key={s.v} value={String(s.v)}>{t(s.key)}</option>)}
      </select>
      <div className="df-seg" role="radiogroup" aria-label={t("demoflow.speed")}>
        {SPEEDS.map((s) => (
          <button key={s.v} type="button" role="radio" aria-checked={Math.abs(speed - s.v) < 0.5} disabled={!running || busy}
            className={cn("df-seg-btn", Math.abs(speed - s.v) < 0.5 && "df-seg-on")} onClick={() => set({ speed: s.v })}>
            {t(s.key)}
          </button>
        ))}
      </div>
      </>}
    </div>
  );
}

// The public link: once the invoice has gone out, the customer can settle it from the map.
function PayButton({ run, onPay, t }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const p = run?.payment;
  if (!p?.available || p.status === "paid") return null;
  const queued = p.status === "queued";
  const pay = async () => {
    setBusy(true); setErr(null);
    try { await onPay(); } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  return (
    <div className="df-pay">
      <Button type="button" variant="accent" size="sm" onClick={pay} disabled={busy || queued}>
        <Banknote aria-hidden /> {queued ? t("demoflow.pay.queued") : t("demoflow.pay.simulate")}
      </Button>
      {err ? <span role="alert" className="df-pay-err">{t(err.status === 409 ? "demoflow.pay.notYet" : "demoflow.pay.failed")}</span> : null}
    </div>
  );
}

function Feed({ model, onSelect, locale, t }) {
  const texts = useTexts();
  const nodeLabel = useNodeLabel();
  // Clock changes and the simulators' own bookkeeping stay in the node panel; the feed shows the work.
  const items = model.events.filter((ev) => ev.kind !== "clock" && !(ev.kind === "tool" && ev.simulated)).slice(-60).reverse();
  return (
    <aside className="df-panel" aria-label={t("demoflow.feed")}>
      <header className="df-panel-head df-feed-head">
        <h2>{t("demoflow.feed")}</h2>
      </header>
      <ol className="df-panel-body df-feed" aria-live="polite">
        {items.length === 0 ? <li className="df-muted df-p">{t("demoflow.feedEmpty")}</li> : null}
        {items.map((ev) => {
          const node = ev.node && !isRunEvent(ev) && model.nodes[ev.node];
          return (
            <li key={ev.seq}>
              <button type="button" className="df-feed-item" onClick={() => node && onSelect(ev.node)} disabled={!node}>
                <span className={cn("df-dot", `df-dot-${ev.state || "tick"}`)} aria-hidden />
                <span className="df-feed-main">
                  <span className="df-feed-title">{texts.title(ev)}</span>
                  <span className="df-feed-meta">{node ? `${nodeLabel(ev.node, node)} · ` : ""}{clockTime(ev.sim_at || ev.at, locale)}</span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}

function DemoFlowPageInner({ token = null }) {
  const { runId } = useParams();
  const { t, locale } = useT();
  const texts = useTexts();
  const pub = Boolean(token);
  const { run, model, live, error, conn, setClock, simulatePayment, reload } = useRunStream(runId, token);
  const [selected, setSelected] = useState(null);
  const [full, setFull] = useState(false);
  // The map gets the full width by default; the live activity rail is one click away.
  const [rail, setRail] = useState(() => localStorage.getItem("huo.demoflow.rail") === "on");
  const rootRef = useRef(null);
  const s = useMemo(() => summary(model), [model]);
  const lastSimAt = model.events.length ? model.events[model.events.length - 1].sim_at : null;
  const latest = [...model.events].reverse().find((ev) => ev.kind !== "clock" && !(ev.kind === "tool" && ev.simulated));

  useEffect(() => {
    const on = () => setFull(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", on);
    return () => document.removeEventListener("fullscreenchange", on);
  }, []);
  const toggleFull = () => {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else rootRef.current?.requestFullscreen?.();
  };
  const toggleRail = () => setRail((v) => { localStorage.setItem("huo.demoflow.rail", v ? "off" : "on"); return !v; });
  const close = useCallback(() => {
    setSelected((id) => {
      // Return focus to the node the panel was opened from.
      if (id) requestAnimationFrame(() => document.querySelector(`[data-node="${CSS.escape(id)}"]`)?.focus());
      return null;
    });
  }, []);
  const select = useCallback((id) => setSelected((cur) => (cur === id ? null : id)), []);

  if (error && !run) return <div className="p-4"><ErrorState error={error} onRetry={reload} /></div>;

  // A stopped or failed run says so, whatever stage its events reached; otherwise the events lead.
  const ended = ["stopped", "failed", "interrupted"].includes(run?.status) || Boolean(model.ended);
  const endedAs = ["stopped", "failed", "interrupted"].includes(run?.status) ? run.status : model.ended?.state === "failed" ? "failed" : "stopped";
  // Delivered and paid with one item still with a person: complete, but neutral rather than a green "Paid".
  const openItem = (model.endReason || run?.end_reason) === "claim_review_handoff";
  // The quote agent may not send the offer itself: it waits on an approval card for a person.
  const offer = model.nodes.offer;
  const awaitingApproval = run?.status === "awaiting_approval" || (offer?.state === "waiting" && offer?.titleEv?.data?.title_key === "offer.awaiting_approval");
  const status = ended ? endedAs : model.phase === "done" ? (openItem ? "done_open" : "done") : model.phase === "lifecycle" ? "lifecycle"
    : awaitingApproval ? "awaiting_approval" : PHASE_BADGE[run?.status] ? run.status : "running";
  const lane = run?.lane;
  return (
    // data-last-seq: the newest event the map has applied (recorders step a replay on it).
    <div ref={rootRef} className={cn("df-root helios-app-stage", ended && "df-ended")} data-last-seq={model.lastSeq || 0}>
      <header className="df-head">
        {pub ? null : <Link to="/ops/demo" className="df-iconbtn" aria-label={t("demoflow.back")}><ArrowLeft /></Link>}
        <div className="df-head-title">
          <h1>
            {lane?.origin?.city || run?.origin_text || "…"} <ArrowRight className="df-arrow" aria-hidden /> {lane?.destination?.city || run?.destination_text || "…"}
          </h1>
          <div className="df-head-sub">
            <span className="df-mono">{run?.ref}</span>
            {lane?.mode ? <span>{t(`demoflow.mode.${lane.mode}`)}</span> : null}
            <StatusBadge size="sm" state={PHASE_BADGE[status] || "in-progress"} label={t(`demoflow.status.${status}`)} />
            {ended || status === "done" || status === "done_open" ? null : <span className={cn("df-conn", `df-conn-${conn}`)}>{t(`demoflow.conn.${conn}`)}</span>}
          </div>
        </div>
        <dl className="df-stats">
          <div><dt>{t("demoflow.stat.partners")}</dt><dd className="df-num">{s.partners}</dd></div>
          <div><dt>{t("demoflow.stat.rates")}</dt><dd className="df-num">{s.rates}{s.excluded ? <span className="df-stat-note">{t("demoflow.stat.excluded", { n: s.excluded })}</span> : null}</dd></div>
          {s.docs ? <div><dt>{t("demoflow.stat.docs")}</dt><dd className="df-num">{s.docs}</dd></div> : null}
          <div><dt>{t("demoflow.stat.offer")}</dt><dd className="df-num">{s.offer ? texts.value(s.offer) : "—"}</dd></div>
        </dl>
        {pub ? <PayButton run={run} onPay={simulatePayment} t={t} /> : null}
        <SimClock run={run} lastSimAt={lastSimAt} stoppedAt={model.ended?.sim_at} ended={ended} onSet={setClock} locale={locale} t={t} readOnly={pub} />
        <LangToggle />
        <button type="button" className="df-iconbtn" onClick={toggleRail} aria-pressed={rail} aria-label={t("demoflow.feed")}><PanelRight /></button>
        <button type="button" className="df-iconbtn" onClick={toggleFull} aria-label={full ? t("demoflow.exitFull") : t("demoflow.full")}>
          {full ? <Minimize2 /> : <Maximize2 />}
        </button>
      </header>
      <div className="df-now-line" aria-live="polite">
        <span className={cn("df-dot", `df-dot-${ended ? (endedAs === "failed" ? "failed" : "tick") : latest?.state || "tick"}`)} aria-hidden style={{ marginTop: 6 }} />
        {latest ? <span key={latest.seq} className="df-now">{texts.line(latest)}</span> : <span className="df-muted">{t("demoflow.feedEmpty")}</span>}
      </div>
      <div className="df-body">
        <FlowMap model={model} live={live} selected={selected} onSelect={select} />
        {selected ? <NodePanel runId={runId} model={model} id={selected} onClose={close} /> : rail ? <Feed model={model} onSelect={select} locale={locale} t={t} /> : null}
      </div>
    </div>
  );
}

export default function DemoFlowPage() {
  return <DemoLocale><DemoFlowPageInner /></DemoLocale>;
}

// /track/:runId?t=<access token>: the customer's own run, no sign-in.
export function PublicFlowPage() {
  const [params] = useSearchParams();
  const token = params.get("t") || "";
  return (
    <RunAccess.Provider value={token}>
      <DemoLocale><DemoFlowPageInner token={token} /></DemoLocale>
    </RunAccess.Provider>
  );
}
