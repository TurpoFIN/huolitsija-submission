"""Self-monitoring: deep health checks every minute, owner alerts, owner-queue SLA escalation and the daily owner
digest e-mail. Nothing here acts for a customer, so these jobs keep running while the Autopilot kill switch is on."""
import logging
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from core import config
from core.agent_runtime import HELSINKI, helsinki_day_start, notify_owner, owner_extra_email, owner_users, spend_today
from core.alerts import raise_alert, resolve_alert
from core.db import db, register_index
from core.events import on
from core.jobs import counts as job_counts
from core.jobs import job, schedule
from core.notify import bi
from core.settings import get_settings
from core.util import NO_ID, iso, new_id, now, now_iso, parse_iso

log = logging.getLogger("monitoring")
register_index("owner_reports", "id", unique=True)
register_index("owner_reports", [("kind", 1), ("created_at", -1)])

WORKER_STALE_SECONDS = 90
QUEUE_LAG_WARN_SECONDS = 300


def day_start_iso() -> str:
    return iso(helsinki_day_start())


def _check(status: str, fi: str, en: str, **data) -> dict:
    return {"status": status, "fi": fi, "en": en, **data}


async def deep_health(store: bool = True) -> dict:
    """Every check: ok | warn | fail, with a sentence in both languages. Stored in db.system 'health' when store."""
    checks: dict[str, dict] = {}
    t0 = time.monotonic()
    try:
        await db.command("ping")
        ms = int((time.monotonic() - t0) * 1000)
        checks["database"] = _check("ok" if ms < 500 else "warn", f"Tietokanta vastaa ({ms} ms)", f"Database answers ({ms} ms)", ms=ms)
    except Exception as exc:  # noqa: BLE001
        checks["database"] = _check("fail", "Tietokanta ei vastaa", "Database does not answer", error=str(exc)[:200])
        return {"status": "fail", "checked_at": now_iso(), "checks": checks}
    settings = await get_settings()
    beat = await db.raw.worker_heartbeats.find_one({}, {"_id": 0}, sort=[("beat_at", -1)])
    age = (now() - parse_iso(beat["beat_at"])).total_seconds() if beat and beat.get("beat_at") else None
    if not config.env_bool("JOBS_WORKER_ENABLED", True):
        checks["worker"] = _check("warn", "Työjono on poistettu käytöstä tällä palvelimella", "The job worker is disabled on this server")
    elif age is None or age > WORKER_STALE_SECONDS:
        checks["worker"] = _check("fail", "Työjonon käsittelijä ei ole kuitannut toimintaansa", "The job worker has not reported in",
                                  seconds=None if age is None else int(age))
    else:
        checks["worker"] = _check("ok", f"Työjono käynnissä ({int(age)} s sitten)", f"Job worker running ({int(age)} s ago)",
                                  seconds=int(age))
    jc = await job_counts()
    lag = jc.get("oldest_due_lag_seconds")
    if lag is not None and lag > QUEUE_LAG_WARN_SECONDS:
        checks["queue"] = _check("warn", f"Jonossa odottaa töitä {lag // 60} minuuttia myöhässä",
                                 f"Due jobs are {lag // 60} minutes late", lag_seconds=lag, **{k: jc[k] for k in ("queued", "retry")})
    else:
        checks["queue"] = _check("ok", f"Jonossa {jc['queued'] + jc['retry']} työtä", f"{jc['queued'] + jc['retry']} jobs queued",
                                 lag_seconds=lag)
    late = await db.raw.job_schedules.count_documents({"enabled": {"$ne": False},
                                                       "next_run_at": {"$lt": iso(now() - timedelta(minutes=10))}})
    checks["schedules"] = (_check("warn", f"{late} ajastettua työtä myöhässä", f"{late} schedules are late", late=late) if late
                           else _check("ok", "Ajastukset ajallaan", "Schedules on time"))
    dl = jc.get("dead_letters_open", 0)
    checks["dead_letters"] = (_check("warn", f"{dl} epäonnistunutta työtä odottaa", f"{dl} dead letters waiting", open=dl) if dl
                              else _check("ok", "Ei epäonnistuneita töitä", "No dead letters"))
    overdue = await db.raw.approvals.count_documents({"status": "pending", "sla_due_at": {"$lt": now_iso()}})
    checks["owner_queue"] = (_check("warn", f"{overdue} hyväksyntää myöhässä", f"{overdue} approvals past their SLA", overdue=overdue)
                             if overdue else _check("ok", "Hyväksynnät ajallaan", "Approvals within SLA"))
    from core.autonomy import active_test_grants
    grants = [{k: g.get(k) for k in ("company_id", "name", "expires_at", "agents")} for g in await active_test_grants()]
    if settings.get("autopilot_halted"):
        checks["autopilot"] = _check("warn", "Hätäpysäytys päällä: agentit odottavat", "Kill switch on: agents are waiting",
                                     test_tenant_grants=grants)
    elif grants and not settings["autopilot_enabled"]:
        # s6 r94: a test-tenant grant is Autopilot running for that tenant, so health names it until it ends
        until = ", ".join(f"{g['name'] or g['company_id']} {g['expires_at']}" for g in grants)
        checks["autopilot"] = _check("ok", f"Autopilotti pois; testiasiakkaan lupa voimassa ({until})",
                                     f"Autopilot off; test-tenant grant active ({until})", test_tenant_grants=grants)
    else:
        checks["autopilot"] = _check("ok", "Autopilotti " + ("päällä" if settings["autopilot_enabled"] else "pois"),
                                     "Autopilot " + ("on" if settings["autopilot_enabled"] else "off"), test_tenant_grants=grants)
    fails = await db.raw.event_failures.count_documents({"at": {"$gte": iso(now() - timedelta(hours=1))}})
    checks["events"] = (_check("warn", f"{fails} tapahtumankäsittelijää epäonnistui tunnin aikana",
                               f"{fails} event handlers failed in the last hour", failures=fails) if fails
                        else _check("ok", "Tapahtumat käsitelty", "Events handled"))
    from core.llm import llm_available
    checks["ai"] = (_check("ok", "Tekoälymalli käytettävissä", "AI model available") if llm_available() else
                    _check("warn", "Tekoälymalli ei ole käytössä: tekoälytyöt menevät omistajalle",
                           "No AI model configured: AI work goes to the owner"))
    from core.email import email_configured
    checks["email"] = (_check("ok", f"Sähköposti: {config.EMAIL_PROVIDER}", f"E-mail: {config.EMAIL_PROVIDER}")
                       if email_configured() else
                       _check("warn", "Sähköpostit tallennetaan lokiin, niitä ei lähetetä", "E-mails are logged, not delivered"))
    checks["payments"] = (_check("ok", "Stripe määritetty", "Stripe configured") if config.STRIPE_API_KEY else
                          _check("warn", "Stripe-avain puuttuu", "Stripe key missing"))
    rep = await db.raw.owner_reports.find_one({"kind": "reconciliation"}, {"_id": 0, "created_at": 1, "status": 1},
                                              sort=[("created_at", -1)])
    if not rep:
        checks["reconciliation"] = _check("warn", "Täsmäytystä ei ole vielä ajettu", "Reconciliation has not run yet")
    elif parse_iso(rep["created_at"]) < now() - timedelta(hours=26):
        checks["reconciliation"] = _check("warn", "Päivän täsmäytys puuttuu", "Daily reconciliation is missing", last=rep["created_at"])
    else:
        checks["reconciliation"] = _check("ok" if rep.get("status") == "ok" else "warn",
                                          "Täsmäytys kunnossa" if rep.get("status") == "ok" else "Täsmäytyksessä poikkeamia",
                                          "Reconciliation clean" if rep.get("status") == "ok" else "Reconciliation found differences",
                                          last=rep["created_at"])
    meta = await db.raw.sanctions_lists.find_one({}, {"_id": 0, "fetched_at": 1}, sort=[("fetched_at", -1)])
    if meta and meta.get("fetched_at") and parse_iso(meta["fetched_at"]) > now() - timedelta(hours=48):
        checks["sanctions_lists"] = _check("ok", "Pakotelistat ajan tasalla", "Sanctions lists current", fetched_at=meta["fetched_at"])
    else:
        checks["sanctions_lists"] = _check("warn", "Pakotelistoja ei ole päivitetty 48 tuntiin", "Sanctions lists not refreshed in 48 hours",
                                           fetched_at=(meta or {}).get("fetched_at"))
    worst = "fail" if any(c["status"] == "fail" for c in checks.values()) else (
        "warn" if any(c["status"] == "warn" for c in checks.values()) else "ok")
    out = {"status": worst, "checked_at": now_iso(), "checks": checks}
    if store:
        await db.raw.system.update_one({"_id": "health"}, {"$set": {"key": "health", **out}}, upsert=True)
    return out


async def latest_health() -> dict | None:
    return await db.raw.system.find_one({"_id": "health"}, {"_id": 0, "key": 0})


# Checks that open an owner alert when they are not ok (key -> severity when failing).
ALERTING = {"worker": "critical", "queue": "warning", "schedules": "warning", "dead_letters": "warning",
            "owner_queue": "warning", "events": "warning", "reconciliation": "warning"}


@job("monitoring.health", max_attempts=2, backoff=(30,), timeout=60, label_fi="Terveystarkistus", label_en="Health check")
async def health_job(j):
    h = await deep_health(store=True)
    for key, sev in ALERTING.items():
        c = h["checks"].get(key)
        if not c:
            continue
        if c["status"] in ("warn", "fail"):
            await raise_alert(f"health:{key}", "critical" if c["status"] == "fail" else sev,
                              bi(c["fi"], c["en"]), bi(c["fi"], c["en"]), data=c)
        else:
            await resolve_alert(f"health:{key}")
    await close_settled_dead_alerts()
    return {"status": h["status"]}


async def close_settled_dead_alerts() -> list[str]:
    """A "Job failed: <kind>" alert closes once no dead letter of that kind is open (a person retried or dismissed it)."""
    closed = []
    async for a in db.raw.alerts.find({"status": "open", "key": {"$regex": "^dead:"}}, {"_id": 0, "key": 1}):
        if not await db.raw.dead_letters.find_one({"kind": a["key"][len("dead:"):], "status": "open"}, {"_id": 1}):
            await resolve_alert(a["key"], "No open dead letter of this kind")
            closed.append(a["key"])
    return closed


schedule("monitoring.health_every_minute", job="monitoring.health", every_seconds=60,
         label_fi="Terveystarkistus minuutin välein", label_en="Health check every minute")()


@on("job.dead")
async def _dead_letter_alert(p):
    j = p["job"]
    await raise_alert(f"dead:{j['kind']}", "warning",
                      bi(f"Työ epäonnistui: {j['kind']}", f"Job failed: {j['kind']}"),
                      bi(f"{j.get('attempts')} yrityksen jälkeen: {p.get('error')}", f"After {j.get('attempts')} attempts: {p.get('error')}"),
                      data={"job_id": j["id"], "dead_letter_id": p["dead_letter"]["id"]})


# ---------------------------------------------------------------------------------------------
# Owner queue SLA timers and escalation
# ---------------------------------------------------------------------------------------------
@job("owner_queue.escalate", max_attempts=3, timeout=120, label_fi="Hyväksyntöjen muistutukset", label_en="Approval reminders")
async def escalate(j):
    settings = await get_settings()
    every = float(settings.get("owner_escalation_hours") or 4)
    t = now_iso()
    n = 0
    async for a in db.raw.approvals.find({"status": "pending", "sla_due_at": {"$lt": t}}, NO_ID):
        last = parse_iso(a.get("escalated_at"))
        if last and now() - last < timedelta(hours=every):
            continue
        level = int(a.get("escalation_level") or 0) + 1
        await db.raw.approvals.update_one({"id": a["id"]}, {"$set": {"escalation_level": level, "escalated_at": t}})
        late_h = int((now() - parse_iso(a["sla_due_at"])).total_seconds() // 3600)
        await notify_owner(bi(f"Myöhässä ({level}. muistutus): {a.get('title_fi') or a.get('title')}",
                              f"Overdue (reminder {level}): {a.get('title')}"),
                           bi(f"Vastausaika ylittyi {late_h} tuntia sitten. {a.get('reason_fi') or ''}".strip(),
                              f"The response time passed {late_h} hours ago. {a.get('reason') or ''}".strip()),
                           f"/ops/approvals?focus={a['id']}", kind="approval", dedupe_key=f"escalate:{a['id']}:{level}")
        if level >= 2:
            await raise_alert(f"approval_overdue:{a['id']}", "warning",
                              bi(f"Hyväksyntä myöhässä: {a.get('title_fi') or a.get('title')}", f"Approval overdue: {a.get('title')}"),
                              bi(f"{level}. muistutus lähetetty", f"Reminder {level} sent"), link=f"/ops/approvals?focus={a['id']}")
        n += 1
    return {"escalated": n}


schedule("owner_queue.escalate_every_5_min", job="owner_queue.escalate", every_seconds=300,
         label_fi="Hyväksyntöjen vastausajat", label_en="Approval response times")()


@on("approval.decided")
async def _resolve_overdue_alert(p):
    await resolve_alert(f"approval_overdue:{p['approval']['id']}", "decided")


# ---------------------------------------------------------------------------------------------
# Daily owner digest
# ---------------------------------------------------------------------------------------------
def _eur(cents: int, lang: str) -> str:
    s = f"{(cents or 0) / 100:,.2f}"
    return (s.replace(",", " ").replace(".", ",") + " €") if lang == "fi" else ("€" + s)


async def build_digest() -> dict:
    end = now()
    start = end - timedelta(hours=24)
    since = iso(start)
    dec = {"auto": 0, "owner_queue": 0, "blocked": 0}
    by_agent: dict[str, dict] = {}
    async for r in db.raw.agent_decisions.aggregate([{"$match": {"at": {"$gte": since}}},
                                                     {"$group": {"_id": {"a": "$agent", "o": "$outcome"}, "n": {"$sum": 1},
                                                                 "fi": {"$first": "$agent_label_fi"}, "en": {"$first": "$agent_label_en"}}}]):
        dec[r["_id"]["o"]] = dec.get(r["_id"]["o"], 0) + r["n"]
        a = by_agent.setdefault(r["_id"]["a"], {"fi": r["fi"], "en": r["en"], "auto": 0, "owner_queue": 0, "blocked": 0})
        a[r["_id"]["o"]] = a.get(r["_id"]["o"], 0) + r["n"]
    pending = [a async for a in db.raw.approvals.find({"status": "pending"}, {"_id": 0, "id": 1, "title": 1, "title_fi": 1,
                                                                              "kind": 1, "sla_due_at": 1, "legal_point": 1})
               .sort("created_at", 1).limit(20)]  # the oldest 20 are listed; the counts below cover every card
    overdue = [a for a in pending if a.get("sla_due_at") and a["sla_due_at"] < now_iso()]
    pending_count = await db.raw.approvals.count_documents({"status": "pending"})
    overdue_count = await db.raw.approvals.count_documents({"status": "pending", "sla_due_at": {"$lt": now_iso()}})
    shipments = {"booked": await db.raw.shipments.count_documents({"created_at": {"$gte": since}}),
                 "delivered": await db.raw.shipments.count_documents({"delivered_at": {"$gte": since}}),
                 "active": await db.raw.shipments.count_documents({"status": {"$nin": ["delivered", "cancelled"]}})}
    issued, issued_cents, paid_cents = 0, 0, 0
    from core.tenant_visibility import finance_scope
    scope = await finance_scope()  # owner money figures never include test tenants
    async for inv in db.raw.invoices.find({**scope, "issued_at": {"$gte": since}, "kind": "invoice"}, {"_id": 0, "total_cents": 1}):
        issued += 1
        issued_cents += inv.get("total_cents") or 0
    async for inv in db.raw.invoices.find({**scope, "paid_at": {"$gte": since}}, {"_id": 0, "total_cents": 1}):
        paid_cents += inv.get("total_cents") or 0
    overdue_cents = 0
    async for inv in db.raw.invoices.find({**scope, "status": "overdue"}, {"_id": 0, "total_cents": 1, "paid_cents": 1, "credited_cents": 1}):
        overdue_cents += max(0, (inv.get("total_cents") or 0) - (inv.get("paid_cents") or 0) - (inv.get("credited_cents") or 0))
    alerts = [a async for a in db.raw.alerts.find({"status": "open"}, {"_id": 0, "title": 1, "title_fi": 1, "severity": 1})]
    health = await latest_health() or {}
    settings = await get_settings()
    return {"from": since, "to": iso(end), "decisions": dec, "by_agent": by_agent, "pending": pending, "overdue": overdue,
            "pending_count": pending_count, "overdue_count": overdue_count,
            "shipments": shipments, "invoices": {"issued": issued, "issued_cents": issued_cents, "paid_cents": paid_cents,
                                                 "overdue_cents": overdue_cents},
            "alerts": alerts, "health": health.get("status"), "spend_today_cents": await spend_today(),
            "spend_limit_cents": settings.get("guard_spend_per_day_cents"), "autopilot_enabled": settings["autopilot_enabled"],
            "halted": settings["autopilot_halted"],
            "dead_letters": await db.raw.dead_letters.count_documents({"status": "open"})}


def digest_lines(d: dict, lang: str) -> list[str]:
    fi = lang == "fi"
    L = []
    if fi:
        L.append(f"Autopilotti {'päällä' if d['autopilot_enabled'] else 'pois'}" + (" · hätäpysäytys päällä" if d["halted"] else "")
                 + f" · järjestelmän tila {d.get('health') or '—'}")
        L.append(f"Agenttien päätökset 24 h: {d['decisions'].get('auto', 0)} tehty itse, {d['decisions'].get('owner_queue', 0)} "
                 f"omistajalle, {d['decisions'].get('blocked', 0)} estetty.")
        L.append(f"Hyväksyttävää: {d['pending_count']}, joista myöhässä {d['overdue_count']}.")
        L += [f"  – {a.get('title_fi') or a.get('title')}" + (" (lakisääteinen)" if a.get("legal_point") else "") for a in d["pending"][:8]]
        L.append(f"Lähetykset: {d['shipments']['booked']} uutta, {d['shipments']['delivered']} toimitettu, {d['shipments']['active']} matkalla.")
        L.append(f"Laskut: {d['invoices']['issued']} lähetetty ({_eur(d['invoices']['issued_cents'], 'fi')}), maksuja "
                 f"{_eur(d['invoices']['paid_cents'], 'fi')}, erääntynyttä {_eur(d['invoices']['overdue_cents'], 'fi')}.")
        L.append(f"Kulutus tänään {_eur(d['spend_today_cents'], 'fi')} / raja {_eur(d['spend_limit_cents'], 'fi')}.")
        L.append(f"Avoimet hälytykset: {len(d['alerts'])}; epäonnistuneet työt: {d['dead_letters']}.")
        L += [f"  – {a.get('title_fi') or a.get('title')}" for a in d["alerts"][:6]]
    else:
        L.append(f"Autopilot {'on' if d['autopilot_enabled'] else 'off'}" + (" · kill switch on" if d["halted"] else "")
                 + f" · system health {d.get('health') or '—'}")
        L.append(f"Agent decisions in 24 h: {d['decisions'].get('auto', 0)} done, {d['decisions'].get('owner_queue', 0)} to the owner, "
                 f"{d['decisions'].get('blocked', 0)} blocked.")
        L.append(f"To approve: {d['pending_count']}, {d['overdue_count']} overdue.")
        L += [f"  – {a.get('title')}" + (" (legally required)" if a.get("legal_point") else "") for a in d["pending"][:8]]
        L.append(f"Shipments: {d['shipments']['booked']} new, {d['shipments']['delivered']} delivered, {d['shipments']['active']} under way.")
        L.append(f"Invoices: {d['invoices']['issued']} issued ({_eur(d['invoices']['issued_cents'], 'en')}), payments "
                 f"{_eur(d['invoices']['paid_cents'], 'en')}, overdue {_eur(d['invoices']['overdue_cents'], 'en')}.")
        L.append(f"Spend today {_eur(d['spend_today_cents'], 'en')} of {_eur(d['spend_limit_cents'], 'en')}.")
        L.append(f"Open alerts: {len(d['alerts'])}; dead letters: {d['dead_letters']}.")
        L += [f"  – {a.get('title')}" for a in d["alerts"][:6]]
    return L


@job("monitoring.digest_daily", max_attempts=4, timeout=120, label_fi="Päivän kooste omistajalle", label_en="Daily owner digest")
async def digest_job(j):
    from core.email import render, send_email
    d = await build_digest()
    day = now().astimezone(HELSINKI).date().isoformat()
    report = {"id": new_id(), "kind": "digest", "created_at": now_iso(), "day": day, "data": d, "sent_to": []}
    link = (config.APP_BASE_URL or "") + "/ops/autopilot"
    for u in await owner_users():
        if not u.get("email"):
            continue
        lang = "en" if u.get("locale") == "en" else "fi"
        subject = ("Huolitsija: päivän kooste " if lang == "fi" else "Huolitsija: daily digest ") + day
        html, text = render(subject, digest_lines(d, lang), link if config.APP_BASE_URL else None,
                            "Avaa Autopilotti" if lang == "fi" else "Open Autopilot")
        r = await send_email(u["email"], subject, html, text, tag="digest", ref={"type": "owner_report", "id": report["id"]})
        report["sent_to"].append({"to": u["email"], "status": r.get("status")})
    extra = await owner_extra_email()
    if extra and extra not in {s["to"] for s in report["sent_to"]}:
        subject = "Huolitsija: päivän kooste " + day
        html, text = render(subject, digest_lines(d, "fi") + [""] + digest_lines(d, "en"), None, None)
        r = await send_email(extra, subject, html, text, tag="digest", ref={"type": "owner_report", "id": report["id"]})
        report["sent_to"].append({"to": extra, "status": r.get("status")})
    await db.raw.owner_reports.insert_one(dict(report))
    return {"report_id": report["id"], "sent": len(report["sent_to"])}


schedule("monitoring.digest_0700", job="monitoring.digest_daily", daily_at="07:00",
         label_fi="Päivän kooste klo 7", label_en="Daily digest at 07:00")()
