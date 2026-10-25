"""Partner-reply simulator. For each partner that got a real rate request, a reply in that partner's voice arrives after
a short delay through Huolitsija's real inbound path (email_channel.dispatch.dispatch_raw -> partner_network ->
partner_parser.parse_reply), exactly like a mail the poller fetched. It never leaves the demo: nothing is sent to anyone.

The only markers are provider_id "demo-sim:<uuid>" and the header X-Huolitsija-Demo-Simulated: 1; Huolitsija's agents
read neither. A real reply from the partner wins: the simulator stays quiet for a thread once a real reply exists.
Behaviour and numbers come from random.Random(f"{run.seed}:{outreach id}:{turn}"), so two runs on one lane differ."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from core.db import db
from core.llm import llm_text
from core.util import new_id

from . import conf
from .events import emit

log = logging.getLogger("demo_autoquote.simulator")
BEHAVIOURS = (("quote", 0.42), ("quote_itemised", 0.2), ("question", 0.16), ("quote_high", 0.1), ("decline", 0.12))

SYSTEM = """You write one realistic business e-mail: a freight company's reply to a rate request from Huolitsija, a
Finnish freight forwarder. Write as the named company's sales or pricing person, in plain English, 50-140 words, no
markdown, no subject line. Use exactly the numbers you are given, written the way a forwarder writes them
(e.g. "EUR 2,850 all-in"). Mention transit time and validity when given. Sign with a plausible first name, role and the
company name. Never mention that you are an AI or that anything is simulated. Output the e-mail body only."""


def rng(run: dict, row_id: str, turn: int) -> random.Random:
    return random.Random(f"{run.get('seed')}:{row_id}:{turn}")


def pick_behaviour(r: random.Random) -> str:
    x, acc = r.random(), 0.0
    for name, w in BEHAVIOURS:
        acc += w
        if x <= acc:
            return name
    return "quote"


async def real_reply_exists(row_id: str) -> bool:
    return bool(await db.partner_messages.find_one({"outreach_id": row_id, "direction": "in",
                                                    "provider_id": {"$not": {"$regex": "^demo-sim:"}}}))


def numbers(r: random.Random, reference: dict, behaviour: str) -> dict:
    """Price, surcharges, transit and validity around Huolitsija's market reference for this lane and cargo."""
    ref_cost = max(15000, int(reference.get("cost_cents") or 0))
    factor = r.uniform(1.25, 1.5) if behaviour == "quote_high" else r.uniform(0.86, 1.14)
    total = int(round(ref_cost * factor / 1000.0)) * 1000  # whole tens of euros
    transit = max(1, int(reference.get("transit_days") or 5) + r.choice([0, 0, 1, 1, 2]))
    valid = (date.today() + timedelta(days=r.choice([7, 10, 14, 14, 21, 30]))).isoformat()
    out = {"total_cents": total, "transit_days": transit, "valid_to": valid}
    if behaviour == "quote_itemised":
        fuel_pct = r.choice([8, 10, 12, 14])
        base = int(round(total / (1 + fuel_pct / 100) / 1000.0)) * 1000
        out.update(base_cents=base, fuel_cents=total - base, fuel_pct=fuel_pct)
    return out


def _eur(cents: int) -> str:
    return f"EUR {cents / 100:,.0f}"


def brief(row: dict, request_text: str, behaviour: str, n: dict, follow_up: str | None) -> str:
    who = f"Company: {row['carrier_name']} ({row.get('country') or '—'}), {row.get('role') or 'logistics company'}."
    base = f"{who}\nTheir rate request from Huolitsija:\n\"\"\"\n{request_text[:2500]}\n\"\"\"\n"
    if follow_up:
        base += f"Huolitsija's latest message to them:\n\"\"\"\n{follow_up[:1500]}\n\"\"\"\n"
    if behaviour == "decline":
        return base + "Write a polite decline: no capacity / not a lane they serve right now. No price."
    if behaviour == "question":
        return base + ("Do not quote yet. Ask exactly one practical question a carrier would need answered before quoting "
                       "(e.g. tail-lift at delivery, loading hours, stackability, customs papers, exact pallet dimensions).")
    if behaviour == "counter_reply":
        if n.get("accept"):
            return base + f"Accept their proposed price of {_eur(n['total_cents'])} all-in. Confirm transit {n['transit_days']} days, valid until {n['valid_to']}."
        return base + (f"Do not accept their number. Offer a small improvement: {_eur(n['total_cents'])} all-in, transit "
                       f"{n['transit_days']} days, valid until {n['valid_to']}.")
    price = (f"base freight {_eur(n['base_cents'])} plus fuel surcharge {n['fuel_pct']}% ({_eur(n['fuel_cents'])}), "
             f"total {_eur(n['total_cents'])}" if n.get("base_cents") else f"{_eur(n['total_cents'])} all-in")
    extra = " Add one realistic condition (e.g. loading within 2 hours, subject to space at booking)." if behaviour != "quote" else ""
    return base + f"Quote: {price}. Transit time {n['transit_days']} days. Valid until {n['valid_to']}.{extra}"


def raw_message(row: dict, subject: str, body: str, *, in_reply_to: str | None, references: list[str]) -> tuple[bytes, str]:
    msg = EmailMessage()
    mid = make_msgid(idstring="sim", domain=(row.get("domain") or "partner.example"))
    msg["Message-ID"] = mid
    msg["Date"] = formatdate(localtime=False)
    msg["From"] = f"{row['carrier_name']} <{row['contact_email']}>"
    msg["To"] = conf.inbound_address()
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        from core.email import fit_references
        msg["References"] = fit_references(" ".join(references))
    msg["X-Huolitsija-Demo-Simulated"] = "1"
    msg.set_content(body)
    return msg.as_bytes(), mid


async def deliver(run: dict, row: dict, *, turn: int, behaviour: str, n: dict, follow_up: str | None,
                  subject: str, in_reply_to: str | None, references: list[str], delay: float) -> dict | None:
    """Wait, write the reply with the model, and put it into Huolitsija's inbound path."""
    await asyncio.sleep(delay)
    current = await db.demo_runs.find_one({"id": run["id"]}, {"_id": 0, "status": 1})
    if not current or current.get("status") != "running" or await real_reply_exists(row["id"]):
        return None
    # Only a partner of this run whose rate request actually went out gets a reply.
    owned = await db.partner_outreach.find_one({"id": row["id"], "demo_run_id": run["id"]}, {"_id": 1})
    rfq = await db.partner_messages.find_one({"outreach_id": row["id"], "direction": "out", "kind": "rfq", "status": "sent",
                                             # positively sent (not held/queued/unknown), by this run or one it took over
                                             "$or": [{"demo_run_id": run["id"]}, {"reused_by_run": run["id"]}]},
                                            {"_id": 0, "text": 1, "status": 1})
    if not owned or not rfq:
        log.info("simulator: no sent rate request for %s in run %s, no reply", row["id"], run["id"])
        return None
    body = await llm_text("demo_partner_simulator", SYSTEM, brief(row, (rfq or {}).get("text") or "", behaviour, n, follow_up),
                          model=conf.sim_model(), max_tokens=700, ref={"type": "demo_run", "id": run["id"], "outreach_id": row["id"]})
    raw, mid = raw_message(row, subject, body.strip(), in_reply_to=in_reply_to, references=references)
    from services.email_channel.dispatch import dispatch_raw
    res = await dispatch_raw(raw, provider_id=f"demo-sim:{new_id()}", provider_authenticated=True, source="demo_simulator")
    await db.partner_outreach.update_one({"id": row["id"]}, {"$push": {"demo_sim": {"turn": turn, "behaviour": behaviour,
                                                                                  "numbers": n, "message_id": mid}}})
    await emit(run["id"], f"p:{row['id']}", "Reply written by the partner simulator", kind="tool", simulated=True,
               data={"tool_calls": [{"tool": "simulator.partner_reply", "model": conf.sim_model(),
                                     "input": behaviour, "output": f"{len(body.split())} words → inbound {res.get('channel')}"}],
                     "facts": {"behaviour": behaviour, **{k: v for k, v in n.items() if k != "accept"}}})
    return {"message_id": mid, "dispatch": res}


def schedule_first_reply(run: dict, row: dict, *, reference: dict, subject: str, rfq_ids: list[str]) -> asyncio.Task | None:
    if not conf.simulator_on():
        return None
    r = rng(run, row["id"], 0)
    behaviour = pick_behaviour(r)
    lo, hi = conf.sim_delay()
    n = numbers(r, reference, behaviour)
    return asyncio.get_running_loop().create_task(deliver(
        run, row, turn=0, behaviour=behaviour, n=n, follow_up=None, subject=subject,
        in_reply_to=rfq_ids[-1] if rfq_ids else None, references=rfq_ids, delay=r.uniform(lo, hi)))


def schedule_follow_up_reply(run: dict, row: dict, *, turn: int, kind: str, text: str, reference: dict, subject: str,
                             thread_ids: list[str], counter_cents: int | None = None, last_offer: dict | None = None) -> asyncio.Task:
    """The agent wrote to a simulated thread: the partner answers that message (never the real partner)."""
    r = rng(run, row["id"], turn)
    if kind == "counteroffer" and counter_cents:
        prev = (last_offer or {}).get("cost_cents") or counter_cents
        accept = r.random() < 0.45
        total = counter_cents if accept else int(round((counter_cents + (prev - counter_cents) * r.uniform(0.4, 0.7)) / 1000.0)) * 1000
        n = {"total_cents": total, "accept": accept, "transit_days": (last_offer or {}).get("transit_days") or reference.get("transit_days") or 5,
             "valid_to": (last_offer or {}).get("valid_to") or (date.today() + timedelta(days=14)).isoformat()}
        behaviour = "counter_reply"
    else:
        behaviour = "quote_itemised" if r.random() < 0.4 else "quote"
        n = numbers(r, reference, behaviour)
    return asyncio.get_running_loop().create_task(deliver(
        run, row, turn=turn, behaviour=behaviour, n=n, follow_up=text, subject=subject,
        in_reply_to=thread_ids[-1] if thread_ids else None, references=thread_ids, delay=r.uniform(12, 35)))
