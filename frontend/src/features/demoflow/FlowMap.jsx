import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, Banknote, Building2, Calculator, CalendarCheck, CheckCircle2, CircleDashed, FileCheck2, FileText, Globe2, Inbox,
  Landmark, MailOpen, MessageCircleQuestion, Network, Package, PackageCheck, Plane, Receipt, Search, Send, Ship, ShieldCheck,
  Train, Truck, User, Warehouse, XCircle, Clock, Stamp,
} from "lucide-react";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { approvalText, daysText, docsLine, expiredText, failText, useTexts } from "./titles";
import { H, W, actorKind, basisChip, docsSub, edgePath, intakeSub, layout, nodeGroup, nodeSub, partnerName, simChip, skeleton } from "./model";

const ICON = {
  requester: User, intake: Inbox, clarify: MessageCircleQuestion, research: Search, outreach: Send, replies: MailOpen,
  pricing: Calculator, offer: FileCheck2, booking: CalendarCheck, docs: FileText, customs: Landmark, exceptions: AlertTriangle,
  delivery: PackageCheck, invoicing: Receipt, payment: Banknote,
};
const ACTOR_ICON = { customer: Building2, carrier: Ship, trucker: Truck, terminal: Warehouse, customs: Landmark, consignee: PackageCheck,
  bank: Banknote, insurer: ShieldCheck, warehouse: Warehouse, broker: Stamp };
const ROLE_ICON = { carrier: Truck, trucker: Truck, haulier: Truck, forwarder: Network, agent: Globe2, airline: Plane, shipping_line: Ship, rail: Train };
const LEG_ICON = { road: Truck, sea: Ship, ocean: Ship, air: Plane, rail: Train };
// Workflow-state glyphs (Factory status vocabulary): active = in-progress, waiting = pending, done = success, failed = failed.
const STATE_GLYPH = { active: CircleDashed, waiting: Clock, done: CheckCircle2, failed: XCircle };

const PACKET_MS = 1100;
const FRESH_MS = 1700;

export function nodeIcon(id, node) {
  const g = nodeGroup(id);
  if (g === "partner") return ROLE_ICON[(node?.meta?.partner?.role || "").toLowerCase()] || Building2;
  if (g === "actor") return ACTOR_ICON[actorKind(id)] || Globe2;
  if (g === "leg") return LEG_ICON[(node?.meta?.facts?.mode || "").toLowerCase()] || Truck;
  return ICON[id] || Package;
}

export function useNodeLabel() {
  const { t } = useT();
  return (id, node) => {
    const g = nodeGroup(id);
    if (g === "partner") return partnerName(node) || t("demoflow.node.partner");
    if (g === "actor") {
      const label = node?.meta?.facts?.name || node?.meta?.name;
      return label || t(`demoflow.actor.${actorKind(id)}`);
    }
    if (g === "leg") {
      const mode = node?.meta?.facts?.mode;
      const n = id.split(":")[1];
      return mode ? t("demoflow.node.legMode", { n, mode: t(`demoflow.mode.${mode}`) }) : t("demoflow.node.leg", { n });
    }
    const key = `demoflow.node.${id}`;
    const s = t(key);
    return s === key ? id : s;
  };
}

function usePrefersReducedMotion() {
  const [r, setR] = useState(() => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!mq) return undefined;
    const on = () => setR(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return r;
}

/** A dot travelling along one edge: data moving between two nodes. */
function Packet({ d, reverse, born, edge }) {
  const pathRef = useRef(null);
  const dotRef = useRef(null);
  useLayoutEffect(() => {
    let raf;
    const p = pathRef.current;
    const dot = dotRef.current;
    if (!p || !dot) return undefined;
    const len = p.getTotalLength();
    const tick = () => {
      const k = Math.min(1, (Date.now() - born) / PACKET_MS);
      const e = 1 - Math.pow(1 - k, 3);
      const pt = p.getPointAtLength((reverse ? 1 - e : e) * len);
      dot.setAttribute("transform", `translate(${pt.x} ${pt.y})`);
      dot.style.opacity = k >= 1 ? "0" : "1";
      if (k < 1) raf = requestAnimationFrame(tick);
    };
    tick();
    return () => cancelAnimationFrame(raf);
  }, [d, reverse, born]);
  return (
    <g>
      <path ref={pathRef} d={d} fill="none" stroke="none" />
      <g ref={dotRef} className="df-packet" data-edge={edge}>
        <circle r="9" className="df-packet-halo" />
        <circle r="4.5" className="df-packet-core" />
      </g>
    </g>
  );
}

// The clearance step done by the customer's broker (data.actor w:broker), never the authority: "Tulliasiamies vapautti · vienti".
function brokerSub(model, t) {
  const n = model.nodes.customs;
  const ev = n?.titleEv;
  if (n?.state !== "done" || ev?.data?.actor?.id !== "w:broker") return null;
  const dir = ev.data?.facts?.direction;
  return [t("demoflow.sub.brokerReleased"), dir === "export" || dir === "import" ? t(`demoflow.dir.${dir}`).toLowerCase() : ""].filter(Boolean).join(" · ");
}

// A rate production already accepted, in one line on the partner card: "Sovittu: 760,00 € · 3 päivää".
function agreedSub(model, id, t, texts) {
  const n = model.nodes[id];
  if (!id.startsWith("p:") || !n?.meta?.agreed || n.meta.agreedSkipped) return null;
  const v = n.titleEv?.data?.title_key === "partner.rate_agreed" ? n.titleEv.data.title_vars || {} : {};
  const a = n.meta.agreed;
  const amount = v.amount != null ? texts.value(`EUR ${Number(v.amount).toFixed(2)}`) : a.cost_cents != null ? texts.value(`EUR ${(a.cost_cents / 100).toFixed(2)}`) : "";
  const days = v.days ?? a.transit_days;
  return t("demoflow.sub.agreedRate", { amount, days: days != null ? daysText(days, t) : "" }).replace(/ · $/, "");
}

function Node({ id, node, box, label, selected, fresh, onSelect, t, subText, texts, skipped, endAs }) {
  const g = nodeGroup(id);
  // A stopped or failed run: the stage it failed on says so, and nothing else is still working.
  const state = endAs || (skipped ? "skipped" : node?.state || "idle");
  const chip = simChip(id, node) || basisChip(id, node);
  const Icon = nodeIcon(id, node);
  const Glyph = STATE_GLYPH[state];
  const used = Boolean(node);
  // "Awaiting reply" when the node is waiting on an e-mail it sent; any other wait (a claim review) says what it waits for.
  const awaitingReply = state === "waiting" && (node?.stateKind === "email_out" || !node?.title);
  const sub = endAs ? subText : skipped ? t("demoflow.sub.customsNone") : awaitingReply ? t("demoflow.awaiting") : subText || nodeSub(id, node, (n) => daysText(n, t), texts.value, (nd) => texts.title(nd.titleEv) || nd.title || "") || (used ? "" : t("demoflow.upcoming"));
  return (
    <button
      type="button"
      data-node={id}
      data-state={state}
      aria-pressed={selected}
      aria-label={`${label}. ${state === "waiting" && !awaitingReply ? t("demoflow.waiting") : t(`demoflow.state.${state}`)}${sub ? `. ${sub}` : ""}${chip ? `. ${t(chip)}` : ""}`}
      onClick={() => onSelect(id)}
      className={cn("df-node", `df-${g}`, `df-s-${state}`, !used && !skipped && "df-unused", selected && "df-selected", fresh && "df-fresh")}
      title={label}
      style={{ left: box.x - box.w / 2, top: box.y - box.h / 2, width: box.w, height: box.h }}
    >
      <span className="df-icon" aria-hidden>
        <Icon />
        {Glyph ? <span className="df-glyph"><Glyph /></span> : null}
      </span>
      <span className="df-text">
        <span className="df-label">{label}</span>
        {(g !== "actor" && sub) || chip ? (
          <span className="df-sub-row">
            {g !== "actor" && sub ? <span className="df-sub">{sub}</span> : null}
            {chip ? <span className="df-chip-sim">{t(chip)}</span> : null}
          </span>
        ) : null}
      </span>
    </button>
  );
}

export default function FlowMap({ model, live, selected, onSelect }) {
  const { t } = useT();
  const texts = useTexts();
  const nodeLabel = useNodeLabel();
  const reduced = usePrefersReducedMotion();
  const wrapRef = useRef(null);
  const [box, setBox] = useState({ w: W, h: H });
  const [now, setNow] = useState(Date.now());

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(([e]) => setBox({ w: e.contentRect.width, h: e.contentRect.height }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const lastBorn = live.length ? live[live.length - 1].born : 0;
  useEffect(() => {
    if (!lastBorn) return undefined;
    setNow(Date.now());
    const iv = setInterval(() => {
      setNow(Date.now());
      if (Date.now() - lastBorn > FRESH_MS + 100) clearInterval(iv);
    }, 120);
    return () => clearInterval(iv);
  }, [lastBorn]);

  // Below 700 px the journey reads top to bottom in one column (partners under outreach) and fits the width.
  const narrow = box.w > 0 && box.w < 700;
  const lay = useMemo(() => layout(model, { narrow }), [model, narrow]);
  const intake = intakeSub(model);
  const failNode = model.ended?.state === "failed" ? model.ended.node : null;
  const docsText = docsLine(docsSub(model), t, texts.locale) || undefined;
  // No customs step: said by the backend (state "skipped"), or the goods were delivered and customs never came up.
  const customsSkipped = model.nodes.customs?.state === "skipped"
    || (!model.nodes.customs && (model.nodes.delivery?.state === "done" || model.phase === "done"));
  const fresh = live.filter((l) => now - l.born < FRESH_MS);
  const freshNodes = new Set(fresh.map((l) => l.ev.node));
  const freshEdges = new Set(fresh.filter((l) => l.ev.edge).map((l) => [l.ev.edge.from, l.ev.edge.to].sort().join("|")));

  // Every edge points the way data moved (the planned order until then); an exchange both ways gets two heads.
  const edges = useMemo(() => {
    const out = new Map();
    skeleton(model, lay).forEach(([a, b]) => {
      const k = [a, b].sort().join("|");
      if (lay.pos[a] && lay.pos[b]) out.set(k, { key: k, from: a, to: b, used: false, world: false, both: false });
    });
    Object.values(model.edges).forEach((e) => {
      if (!lay.pos[e.a] || !lay.pos[e.b]) return;
      const plan = out.get(e.key);
      const dirs = Object.keys(e.dirs || {});
      const both = dirs.length > 1;
      const [from, to] = both && plan ? [plan.from, plan.to] : [e.lastFrom, e.lastTo];
      out.set(e.key, { key: e.key, from, to, used: true, count: e.count, both,
        world: e.world || nodeGroup(e.a) === "actor" || nodeGroup(e.b) === "actor" });
    });
    return [...out.values()].map((e) => ({ ...e, d: edgePath(lay.pos[e.from], lay.pos[e.to], lay) }));
  }, [model, lay]);
  const edgeByKey = useMemo(() => new Map(edges.map((e) => [e.key, e])), [edges]);

  // Fit the whole journey (its bounding box) to the stage; on a phone keep a readable minimum and scroll instead.
  const bb = lay.bbox;
  const bw = bb.x1 - bb.x0;
  const bh = bb.y1 - bb.y0;
  const scale = narrow ? Math.min(box.w / bw, 1.2) || 1 : Math.min(box.w / bw, box.h / bh, 1.3) || 1;
  const offX = Math.max(0, (box.w - bw * scale) / 2) - bb.x0 * scale;
  // The phone column starts at the top and scrolls; the wide map sits centred in the stage.
  const offY = (narrow ? 0 : Math.max(0, (box.h - bh * scale) / 2)) - bb.y0 * scale;
  const band = (k) => (lay.bands ? { top: lay.bands[k].top, height: lay.bands[k].h } : undefined);

  const quoteIds = ["requester", "intake", "clarify", "research", "outreach", ...(model.partners.length ? model.partners : ["p:next"]), "replies", "pricing", "offer"];
  const lifeIds = [...lay.life, "exceptions"];
  const ids = [...quoteIds, ...lifeIds, ...lay.actors, ...model.others].filter((id, i, a) => lay.pos[id] && a.indexOf(id) === i);
  const lifeStarted = model.phase !== "quote" && model.phase !== "offer_sent" ? true : lifeIds.some((id) => model.nodes[id]);

  return (
    <div ref={wrapRef} className="df-stage" data-reduced={reduced ? "1" : undefined}>
      <div className="df-sizer" style={{ width: bw * scale, height: bh * scale }} aria-hidden />
      <div className={cn("df-canvas", lay.narrow && "df-canvas-narrow")} style={{ width: lay.width, height: lay.height, transform: `translate(${offX}px, ${offY}px) scale(${scale})` }}>
        <div className="df-band df-band-quote" style={band("quote")} aria-hidden>
          <span>{t("demoflow.band.quote")}</span>
        </div>
        <div className={cn("df-band df-band-life", lifeStarted && "df-band-on")} style={band("life")} aria-hidden>
          <span>{t("demoflow.band.lifecycle")}</span>
        </div>
        <div className="df-band df-band-world" style={band("world")} aria-hidden>
          <span>{t("demoflow.band.world")}</span>
        </div>
        <svg className="df-edges" width={lay.width} height={lay.height} viewBox={`0 0 ${lay.width} ${lay.height}`} aria-hidden>
          <defs>
            {["plan", "used", "hot"].map((k) => (
              <marker key={k} id={`df-arrow-${k}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto-start-reverse">
                <path d="M 1 1 L 9 5 L 1 9 z" className={`df-arrow-${k}`} />
              </marker>
            ))}
          </defs>
          {edges.map((e) => {
            const hot = freshEdges.has(e.key);
            const m = `url(#df-arrow-${hot ? "hot" : e.used ? "used" : "plan"})`;
            return (
              <path key={e.key} data-edge={e.key} d={e.d} markerEnd={m} markerStart={e.both ? m : undefined}
                className={cn("df-edge", e.used ? "df-edge-used" : "df-edge-plan", e.world && "df-edge-world", hot && "df-edge-hot")} />
            );
          })}
          {!reduced && fresh.filter((l) => l.ev.edge).map((l) => {
            const k = [l.ev.edge.from, l.ev.edge.to].sort().join("|");
            const e = edgeByKey.get(k);
            if (!e) return null;
            return <Packet key={`${l.ev.seq}`} d={e.d} reverse={e.from !== l.ev.edge.from} born={l.born} edge={e.key} />;
          })}
        </svg>
        {ids.map((id) => (
          <Node key={id} id={id} node={model.nodes[id]} box={lay.pos[id]} label={nodeLabel(id, model.nodes[id])} t={t} texts={texts}
            selected={selected === id} fresh={freshNodes.has(id)} onSelect={onSelect}
            subText={id === "intake" && intake ? t(intake.key, { n: intake.n }) : id === "docs" ? docsText : undefined}
            skipped={id === "customs" && customsSkipped}
            endAs={id === failNode ? "failed" : model.ended && model.nodes[id]?.state === "active" ? "stopped" : undefined}
            {...(id === failNode ? { subText: failText(model.ended, t) } : model.ended && model.nodes[id]?.state === "active" ? { subText: t("demoflow.sub.interrupted") } : {})}
            {...(model.nodes[id]?.meta?.expired && !(id === failNode) ? { endAs: "expired", subText: expiredText(model.nodes[id].meta.expired, t, texts.locale) } : {})}
            {...(agreedSub(model, id, t, texts) ? { subText: agreedSub(model, id, t, texts) } : {})}
            {...(id === "customs" && brokerSub(model, t) ? { subText: brokerSub(model, t) } : {})}
            {...(model.nodes[id]?.meta?.agreedSkipped && !model.nodes[id]?.meta?.expired && !(id === failNode) ? { endAs: "expired", subText: t("demoflow.ev.partner.agreed_rate_skipped") } : {})}
            {...(id === "offer" && model.nodes.offer?.state === "waiting" && model.nodes.offer.titleEv?.data?.title_key === "offer.awaiting_approval"
              ? { subText: approvalText(model.nodes.offer.titleEv, t) } : {})} />
        ))}
      </div>
    </div>
  );
}
