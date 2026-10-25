// The flow map's only data source: the run record plus its event stream (routes/demo_autoquote.py).
// GET /api/demo/runs/{id}?events=false for the run, then SSE /api/demo/runs/{id}/stream (history first, then live,
// `event: step`, `id: <seq>`). EventSource reconnects on its own and resumes from Last-Event-ID; a watchdog covers
// the streams it cannot see die.
// The public flow link (/track/:runId?t=) reads the same run through /api/demo/public with its access token.
import { createContext, useCallback, useEffect, useReducer, useRef, useState } from "react";
import { apiGet, apiPost } from "@/lib/api";
import { applyEvent, emptyModel, isRunEvent } from "./model";

const ROOT = `${(process.env.REACT_APP_BACKEND_URL || "").replace(/\/$/, "")}/api`;
const BASE = `${ROOT}/demo`;
const PUBLIC = `${ROOT}/demo/public`;

// The public link's access token; null on the internal map.
export const RunAccess = createContext(null);

const query = (token, params = {}) => {
  const s = new URLSearchParams(token ? { ...params, access_token: token } : params).toString();
  return s ? `?${s}` : "";
};

function reducer(state, action) {
  if (action.type === "reset") return { model: emptyModel(), live: [] };
  if (action.type === "event") {
    const model = applyEvent(state.model, action.ev);
    if (model === state.model) return state;
    // Events that arrive after the history replay animate (packets along edges); history only sets state.
    const live = action.animate ? [...state.live.slice(-40), { ev: action.ev, born: Date.now() }] : state.live;
    return { model, live };
  }
  return state;
}

export function useRunStream(runId, token = null) {
  const path = token ? "/demo/public" : "/demo";
  const [{ model, live }, dispatch] = useReducer(reducer, null, () => ({ model: emptyModel(), live: [] }));
  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);
  const [conn, setConn] = useState("connecting"); // connecting | live | reconnecting
  const historyDone = useRef(false);

  const loadRun = useCallback(async () => {
    try {
      const r = await apiGet(`${path}/runs/${runId}${query(token, { events: "false" })}`);
      setRun(r.run);
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, [runId, path, token]);

  useEffect(() => {
    dispatch({ type: "reset" });
    historyDone.current = false;
    setRun(null);
    loadRun();
    // The public link polls /events?after=: a quick tunnel in front of the public host holds SSE bodies back.
    if (token) {
      let after = 0;
      let first = true;
      let stop = false;
      const tick = async () => {
        try {
          // Stored text as recorded (like the SSE stream): the map translates by its own EN/FI toggle, not the page's.
          const r = await apiGet(`${path}/runs/${runId}/events${query(token, { after })}`, { locale: "en" });
          if (stop) return;
          for (const ev of r.items || []) {
            if ((ev.seq || 0) <= after) continue;
            after = ev.seq || after;
            dispatch({ type: "event", ev, animate: !first });
            if (!first && (ev.kind === "clock" || ev.kind === "phase" || isRunEvent(ev))) loadRun();
          }
          first = false;
          historyDone.current = true;
          setConn("live");
        } catch (e) {
          if (!stop) setConn("reconnecting");
        }
      };
      tick();
      const poll = setInterval(tick, 2500);
      const runPoll = setInterval(loadRun, 10000);
      return () => { stop = true; clearInterval(poll); clearInterval(runPoll); };
    }
    let es = null;
    let quietTimer = null;
    let lastSeq = 0;
    let lastMsgAt = Date.now();
    // A burst (the history, or what a reconnect catches up on) only sets state; once the stream goes quiet for a
    // moment, later events are live and animate.
    const settle = () => {
      clearTimeout(quietTimer);
      quietTimer = setTimeout(() => { historyDone.current = true; }, 350);
    };
    const open = (after) => {
      es?.close();
      historyDone.current = false;
      settle();
      es = new EventSource(`${ROOT}${path}/runs/${runId}/stream${query(token, after ? { after } : {})}`, { withCredentials: true });
      es.onopen = () => { lastMsgAt = Date.now(); setConn("live"); };
      es.onerror = () => setConn(es.readyState === EventSource.CLOSED ? "closed" : "reconnecting");
      es.addEventListener("step", (msg) => {
        let ev;
        try { ev = JSON.parse(msg.data); } catch (e) { return; }
        lastMsgAt = Date.now();
        lastSeq = Math.max(lastSeq, ev.seq || 0);
        setConn("live");
        dispatch({ type: "event", ev, animate: historyDone.current });
        if (!historyDone.current) settle();
        // The run record carries the clock and the status (a stop can come as a phase event or a run-level note).
        if (ev.kind === "clock" || ev.kind === "phase" || isRunEvent(ev)) loadRun();
      });
    };
    open(0);
    // Watchdog. The server's heartbeats are SSE comments the page never sees, so a stream that died behind a proxy
    // (a backend restart) looks quiet, not closed. Compare with the run record: behind and silent, or closed, reopens
    // from the last event received. The same poll keeps the status and clock current.
    const watch = setInterval(async () => {
      let r;
      try { r = (await apiGet(`${path}/runs/${runId}${query(token, { events: "false" })}`)).run; } catch (e) { return; }
      setRun(r);
      const behind = Number(r?.event_seq || 0) > lastSeq;
      if (es.readyState === EventSource.CLOSED || (behind && Date.now() - lastMsgAt > 8000)) {
        setConn("reconnecting");
        open(lastSeq);
      }
    }, 10000);
    return () => { clearTimeout(quietTimer); clearInterval(watch); es?.close(); };
  }, [runId, path, token, loadRun]);

  const setClock = useCallback(async (patch) => {
    const r = await apiPost(`/demo/runs/${runId}/clock`, patch);
    setRun((cur) => (cur ? { ...cur, clock: r.clock } : cur));
    return r;
  }, [runId]);

  // Public runs only: the payment once the invoice has gone out. 409 until then.
  const simulatePayment = useCallback(async () => {
    const r = await apiPost(`/demo/public/runs/${runId}/simulate-payment${query(token)}`, { invoice_id: run?.payment?.invoice_id || undefined });
    setRun((cur) => (cur ? { ...cur, payment: { ...(cur.payment || {}), status: r.status, invoice_id: r.invoice_id } } : cur));
    return r;
  }, [runId, token, run?.payment?.invoice_id]);

  return { run, model, live, error, conn, setClock, simulatePayment, reload: loadRun };
}

export const fileUrl = (runId, fileId, token = null) =>
  token ? `${PUBLIC}/runs/${runId}/files/${fileId}${query(token)}` : `${BASE}/runs/${runId}/files/${fileId}`;
