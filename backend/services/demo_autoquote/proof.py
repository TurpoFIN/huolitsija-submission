"""Proof pack of one demo run, read back from where things actually are: the sent folder (email_outbox and the
provider's receipt ids), the requester mailbox (mail.tm, read again now), Huolitsija's inbound records, the stored
quote and its PDF, the AI calls (ai_runs) and the run's event log.

    python -m services.demo_autoquote.proof <run_id> <new empty output directory> [note ...]
"""
from __future__ import annotations

import asyncio
import email
from email.header import decode_header, make_header
import json
import sys
from pathlib import Path

from core.db import db

from . import conf
from .mail import requester_box


def _md_email(e: dict) -> str:
    head = "\n".join(f"- **{k}:** {v}" for k, v in e.items() if k not in ("text",) and v not in (None, "", []))
    return f"{head}\n\n```\n{(e.get('text') or '').strip()[:6000]}\n```\n"


async def export(run_id: str, out: Path, notes: list[str] | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    run = await db.demo_runs.find_one({"id": run_id}, {"_id": 0})
    if not run:
        raise SystemExit(f"unknown run {run_id}")
    events = await db.demo_run_events.find({"run_id": run_id}, {"_id": 0}).sort("seq", 1).to_list(10000)
    (out / "events.jsonl").write_text("".join(json.dumps(e, default=str) + "\n" for e in events))
    lines = [f"# Agent trace {run['ref']}", "", f"Run {run_id} · started {run['created_at']} · status {run['status']}", "",
             "| t (s) | node | state | event | simulated |", "|---:|---|---|---|---|"]
    for e in events:
        edge = f" ({e['edge']['from']} → {e['edge']['to']})" if e.get("edge") else ""
        lines.append(f"| {e['t']} | {e['node']} | {e.get('state') or ''} | {e['title']}{edge} | {'yes' if e.get('simulated') else ''} |")
    (out / "trace.md").write_text("\n".join(lines) + "\n")

    # Sent folder: everything this run sent, as the app recorded it, with the provider's receipt.
    sent = await db.email_outbox.find({"$or": [{"demo_run_id": run_id}, {"ref.id": run_id}, {"world.run_id": run_id}]},
                                      {"_id": 0}).sort("created_at", 1).to_list(2000)
    thread_ids = [run.get("thread_id")] if run.get("thread_id") else []
    if thread_ids:
        sent += [r for r in await db.email_outbox.find({"ref.id": {"$in": thread_ids}}, {"_id": 0}).sort("created_at", 1).to_list(200)
                 if r["id"] not in {x["id"] for x in sent}]
    sent.sort(key=lambda r: r.get("created_at") or "")
    md = [f"# Sent folder {run['ref']}", "", f"{len(sent)} messages in email_outbox for this run (provider receipts in provider_id).", ""]
    for r in sent:
        md += [f"## {r.get('created_at')} · {r.get('tag')} → {r.get('to')}", _md_email({
            "subject": r.get("subject"), "from": r.get("from"), "to": r.get("to"), "original recipient": r.get("to_original"),
            "provider": r.get("provider"), "status": r.get("status"), "provider receipt": r.get("provider_id"),
            "captured for world actor": (r.get("world") or {}).get("actor"),
            "message ids": ", ".join(r.get("message_ids") or []), "attachments": ", ".join(a.get("filename") for a in r.get("attachments") or []),
            "text": r.get("text")})]
    (out / "sent-folder.md").write_text("\n".join(md))

    # Recipients: every external address, why it was picked and where it was published.
    rows = await db.partner_outreach.find({"$or": [{"demo_run_id": run_id}, {"reused_from_run": run_id}]},
                                          {"_id": 0}).sort("created_at", 1).to_list(50)
    reuse = run.get("rfq_reuse") or {}
    taken = await db.demo_runs.find_one({"rfq_reuse.from_run": run_id}, {"_id": 0, "id": 1, "ref": 1})
    rec = [f"# Recipients {run['ref']}", "", f"{len(rows)} partners contacted (external recipients this run: {len(run.get('recipients') or [])}, "
           f"cap {conf.HARD_RECIPIENT_CAP}). Delivery: {'rehearsal mailbox (sandbox)' if run.get('sandbox_partners') else 'the partner address'}."
           + (f" Rate requests taken over from {reuse['from_ref']}: {len(reuse.get('outreach_ids') or [])}, new: {reuse.get('new_rfqs', 0)}."
              if reuse else "") + (f" These rate requests were taken over by {taken['ref']} (same shipment, no second request)."
                                   if taken else ""), "",
           "| partner | role | country | address | published at | found by | why |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        rec.append(f"| {r['carrier_name']} | {r.get('role') or ''} | {r.get('country') or ''} | {r['contact_email']} | "
                   f"{r.get('contact_source_url') or ''} | {r.get('found_by') or ''} | {(r.get('reason') or '').replace('|', '/')} |")
    (out / "recipients.md").write_text("\n".join(rec) + "\n")

    # Partner replies as Huolitsija's inbound pipeline stored and parsed them.
    msgs = await db.partner_messages.find({"outreach_id": {"$in": [r["id"] for r in rows]}}, {"_id": 0}).sort("seq", 1).to_list(500)
    by = {r["id"]: r["carrier_name"] for r in rows}
    rp = [f"# Partner threads {run['ref']}", ""]
    for m in msgs:
        sim = str(m.get("provider_id") or "").startswith("demo-sim:") or m.get("provider") == "demo-simulator"
        rp += [f"## {m.get('at')} · {m.get('direction')} · {by.get(m.get('outreach_id'))} · {m.get('kind') or ''}" + (" · simulated" if sim else ""),
               _md_email({"subject": m.get("subject"), "from": m.get("from"), "to": m.get("to"), "status": m.get("status"),
                          "parsed": json.dumps(m.get("parsed"), default=str) if m.get("parsed") else None, "text": m.get("text")})]
    (out / "partner-threads.md").write_text("\n".join(rp))

    # Requester mailbox, read again now.
    box = requester_box()
    rb = [f"# Requester mailbox {conf.requester_address()} (read back {run['ref']})", ""]
    if box:
        for m in await box.messages():
            if run["ref"] not in (m.get("subject") or ""):
                continue
            msg = email.message_from_bytes(await box.source(m["id"]))
            text = ""
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    break
            atts = [p.get_filename() for p in msg.walk() if p.get_filename()]
            subject = str(make_header(decode_header(msg.get("Subject") or "")))
            rb += [f"## {m.get('createdAt')} · {subject}", _md_email({
                "from": msg.get("From"), "to": msg.get("To"), "reply-to": msg.get("Reply-To"), "message-id": msg.get("Message-ID"),
                "in-reply-to": msg.get("In-Reply-To"), "attachments": ", ".join(atts), "text": text})]
    (out / "requester-mailbox.md").write_text("\n".join(rb))

    # Quote + PDF
    files = []
    basis = None
    if run.get("quote_id"):
        q = await db.quotes.find_one({"id": run["quote_id"]}, {"_id": 0, "claim_token": 0})
        basis = next((o.get("price_basis") for o in (q or {}).get("options") or [] if o.get("recommended")), None)
        (out / "quote.json").write_text(json.dumps(q, indent=2, default=str))
        fid = (run.get("offer") or {}).get("file_id")
        if fid:
            from core.storage import get_file
            data, name, _ = await get_file(fid)
            (out / name).write_bytes(data)
            files.append(name)
    # Every document the run's events point at (offer PDF, booking, customs, POD, invoice ...), read back from storage.
    seen = set(files)
    docs_md = [f"# Documents {run['ref']}", "", "| event | node | document | file |", "|---|---|---|---|"]
    for e in events:
        for d in (e.get("data") or {}).get("documents") or []:
            fid = d.get("file_id")
            if not fid or fid in seen:
                continue
            seen.add(fid)
            try:
                from core.storage import get_file
                data, name, _ = await get_file(fid)
                name = f"doc-{len(seen):02d}-{name}"
                (out / name).write_bytes(data)
                files.append(name)
            except Exception as exc:  # noqa: BLE001 - list it anyway
                name = f"(not readable: {type(exc).__name__})"
            docs_md.append(f"| {e['seq']} | {e['node']} | {d.get('title') or d.get('kind') or ''} | {name} |")
    (out / "documents.md").write_text("\n".join(docs_md) + "\n")
    ai = await db.ai_runs.find({"$or": [{"ref.id": run_id}, {"ref.type": "partner_outreach", "ref.id": {"$in": [r["id"] for r in rows]}}]},
                               {"_id": 0, "id": 1, "agent": 1, "model": 1, "status": 1, "latency_ms": 1, "created_at": 1}).sort("created_at", 1).to_list(500)
    (out / "ai-calls.md").write_text("# AI calls\n\n| at | agent | model | status | ms |\n|---|---|---|---|---:|\n" + "\n".join(
        f"| {a.get('created_at')} | {a.get('agent')} | {a.get('model')} | {a.get('status')} | {a.get('latency_ms')} |" for a in ai) + "\n")
    rfqs = [r for r in sent if r.get("tag") == "demo_rfq"]
    replies = [m for m in msgs if m.get("direction") == "in"]
    simulated = [m for m in replies if str(m.get("provider_id") or "").startswith("demo-sim:") or m.get("provider") == "demo-simulator"]
    summary = {"run": run_id, "ref": run["ref"], "build": run.get("build"), "builds": run.get("builds") or [],
               "notes": list(dict.fromkeys((run.get("notes") or []) + (notes or []))), "events": len(events), "sent": len(sent),
               "rfqs": {"sent_real": sum(1 for r in rfqs if r.get("provider") == "smtp2go" and r.get("status") == "sent"),
                        "captured": sum(1 for r in rfqs if r.get("provider") == "world"), "recipients": len(run.get("recipients") or []),
                        "cap": conf.HARD_RECIPIENT_CAP,
                        **({"reused_from": {"run": reuse["from_run"], "ref": reuse["from_ref"], "rfqs": len(reuse.get("outreach_ids") or []),
                                            "new_rfqs": reuse.get("new_rfqs", 0)}} if reuse else {}),
                        **({"taken_over_by": {"run": taken["id"], "ref": taken["ref"]}} if taken else {})},
               "partner_replies": {"real": len(replies) - len(simulated), "simulated": len(simulated)},
               "partners": len(rows), "partner_messages": len(msgs), "ai_calls": len(ai), "files": files, "quote": run.get("quote_number"),
               **({"price_basis": basis} if basis else {}),
               **({"price_label": "Priced from market rates · no partner rate"} if basis == "market_reference" else {})}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    print(json.dumps(asyncio.get_event_loop().run_until_complete(export(sys.argv[1], Path(sys.argv[2]), sys.argv[3:])), indent=2))
