"""UN/EDIFACT VERMAS (Verified Gross Mass) D.16A, segment layout of the SMDG VERMAS MIG 1.0.

Sent as an attachment with the VGM e-mail to carriers' documentation desks that take EDI by mail (and the
same file is what an INTTRA/e2open-style VGM portal uploads). The DCSA VGM 1.0 JSON goes alongside it.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _esc(s: str) -> str:
    out = []
    for ch in str(s or ""):
        if ch in "?+:'":
            out.append("?")
        out.append(ch)
    return "".join(out)


def _dt(iso: str) -> str:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return d.strftime("%Y%m%d%H%M")


def vermas(*, interchange_ref: str, sender_id: str, recipient_id: str, booking_ref: str, carrier_code: str,
           containers: list[dict], responsible_party: str, signatory: str, contact_email: str, at: str) -> str:
    """containers: [{equipment_reference, iso_type, seal_number?, vgm: {weight_kg, method, weighed_at?}}]."""
    now = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(timezone.utc)
    seg = [f"UNB+UNOC:3+{_esc(sender_id)}+{_esc(recipient_id)}+{now:%y%m%d}:{now:%H%M}+{_esc(interchange_ref)}'"]
    body = [
        f"UNH+1+VERMAS:D:16A:UN:SMDG10'",
        f"BGM+XXX+{_esc(interchange_ref)}+9'",
        f"DTM+137:{_dt(at)}:203'",
        f"NAD+CA+{_esc(carrier_code)}:160:ZZZ'",
    ]
    for c in containers:
        v = c["vgm"]
        body += [
            f"EQD+CN+{_esc(c['equipment_reference'])}+{_esc(c.get('iso_type') or '')}:6346:5++2+5'",
            f"RFF+BN:{_esc(booking_ref)}'",
        ]
        if c.get("seal_number"):
            body.append(f"SEL+{_esc(c['seal_number'])}+SH'")
        body += [
            f"MEA+AAE+VGM+KGM:{int(round(float(v['weight_kg'])))}'",
            f"DTM+798:{_dt(v.get('weighed_at') or at)}:203'",
            f"DOC+{_esc(v['method'])}:VGM:306'",
            f"NAD+SPC+++{_esc(responsible_party[:35])}'",
            f"CTA+RP+:{_esc(signatory[:35])}'",
            f"COM+{_esc(contact_email)}:EM'",
        ]
    body.append(f"UNT+{len(body) + 1}+1'")
    return "\n".join(seg + body + [f"UNZ+1+{_esc(interchange_ref)}'"]) + "\n"
