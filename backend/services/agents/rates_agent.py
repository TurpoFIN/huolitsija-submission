"""Rates agent (daily 05:30 Helsinki): keeps the price base current.

- SDR rate from the IMF (claims liability caps) refreshed.
- Rate cards that end within rate_expiry_warning_days with no successor for the same carrier, mode and lane: one owner
  card per carrier (rate_update) to renew them; expired cards are already left out of pricing (services.pricing).
- Carrier rates come from the carrier contract; the agent never invents a price."""
import json
from datetime import timedelta

from core.agent_runtime import decide, policy_reason
from core.db import db
from core.jobs import job, schedule
from core.settings import get_settings
from core.util import NO_ID

AGENT = "rates_agent"


def _end(v) -> str:
    """A rate card endpoint as a hashable key: the UN/LOCODE, else the country, else the stored value as JSON."""
    if isinstance(v, dict):
        return v.get("unlocode") or v.get("code") or v.get("country") or json.dumps(v, sort_keys=True, default=str)
    return str(v or "")


def _lane(card: dict) -> tuple:
    return (card.get("carrier_id"), card.get("mode"), _end(card.get("origin_country") or card.get("origin")),
            _end(card.get("destination_country") or card.get("destination")), card.get("service"))


@job("rates.update", max_attempts=3, timeout=300, label_fi="Hinnaston päivitys", label_en="Rate update")
async def update_rates(j=None):
    from services import sdr
    from services.billing import today_fi
    out = {}
    try:
        r = await sdr.get_rate(max_age_hours=12)
        out["sdr"] = {"date": (r or {}).get("date"), "stale": (r or {}).get("stale")}
    except Exception as exc:  # noqa: BLE001
        out["sdr"] = {"error": str(exc)[:200]}
    settings = await get_settings()
    today = today_fi()
    horizon = (today + timedelta(days=int(settings.get("rate_expiry_warning_days") or 14))).isoformat()
    cards = await db.raw.rate_cards.find({"archived": {"$ne": True}, "demo": {"$ne": True}}, NO_ID).to_list(20000)
    later = {}
    for c in cards:
        later.setdefault(_lane(c), []).append(c.get("valid_to") or "9999-12-31")
    ending: dict[str, list[dict]] = {}
    for c in cards:
        vt = (c.get("valid_to") or "")[:10]
        if not vt or vt < today.isoformat() or vt > horizon:
            continue
        if any(v[:10] > vt for v in later.get(_lane(c), [])):
            continue  # a successor card already covers the lane
        ending.setdefault(c.get("carrier_id") or "?", []).append(c)
    cards_out = []
    for carrier_id, rows in ending.items():
        carrier = await db.raw.carriers.find_one({"id": carrier_id}, {"_id": 0, "name": 1}) or {}
        name = carrier.get("name") or carrier_id
        first = min(r.get("valid_to")[:10] for r in rows)
        d = await decide(AGENT, "renew_rates", subject=("carrier", carrier_id), subject_label=name,
                         inputs={"cards": [{"id": r.get("id"), "mode": r.get("mode"), "valid_to": r.get("valid_to")} for r in rows[:50]]},
                         extra_reasons=[policy_reason("rates_ending", f"{len(rows)} hintaa päättyy {first} alkaen, uusia ei ole",
                                                      f"{len(rows)} rate(s) end from {first} with no successor")],
                         approval_kind="rate_update", title=f"Renew {name} rates ending {first}",
                         title_fi=f"Uusi kuljetusliikkeen {name} hinnat (päättyvät {first})",
                         link=f"/ops/carriers/{carrier_id}", approval_data={"carrier_id": carrier_id, "count": len(rows)})
        cards_out.append({"carrier": name, "ending": len(rows), "approval_id": d.approval and d.approval["id"]})
    out["ending"] = cards_out
    return out


schedule("rates.daily", job="rates.update", daily_at="05:30", label_fi="Hinnaston päivitys joka aamu",
         label_en="Daily rate update")()
