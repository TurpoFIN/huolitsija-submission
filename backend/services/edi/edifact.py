"""UN/EDIFACT syntax (ISO 9735, syntax version 4, character set UNOC): parse and write interchanges.

    ic = parse(raw)                         # -> Interchange(header, messages[Message(ref, type, segments)], trailer)
    ic.messages[0].first("BGM").el(2)       # element 2 of the first BGM (1-based like the standard)
    raw = write_interchange(sender, recipient, [("IFTMCS", "D", "04A", segments)], control_ref="00000012")

A segment is (tag, [element, ...]); an element is a str or a list of components. Delimiters come from the UNA service
string advice when present, else the ISO 9735 defaults  :+.? '  . The release character escapes every delimiter and
itself, on parse and on write. UNT carries the segment count of its message (UNH to UNT inclusive), UNZ the number of
messages; both are checked on parse (`InterchangeError`)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULT_DELIMS = {"component": ":", "element": "+", "decimal": ".", "release": "?", "segment": "'"}


class InterchangeError(ValueError):
    """The interchange breaks the syntax rules (envelope counts, references, characters)."""


Element = str | list[str]


@dataclass
class Segment:
    tag: str
    elements: list[Element] = field(default_factory=list)

    def el(self, n: int, comp: int | None = None, default: str = "") -> str:
        """Element n (1-based), optionally component comp (1-based). Missing parts give `default`."""
        if n < 1 or n > len(self.elements):
            return default
        e = self.elements[n - 1]
        parts = e if isinstance(e, list) else [e]
        if comp is None:
            return (parts[0] if parts else "") or default
        return (parts[comp - 1] if 0 < comp <= len(parts) else "") or default

    def comps(self, n: int) -> list[str]:
        if n < 1 or n > len(self.elements):
            return []
        e = self.elements[n - 1]
        return e if isinstance(e, list) else [e]


@dataclass
class Message:
    ref: str
    type: str
    version: str
    release: str
    agency: str
    association: str
    segments: list[Segment]  # without UNH / UNT

    def all(self, tag: str) -> list[Segment]:
        return [s for s in self.segments if s.tag == tag]

    def first(self, tag: str, **where) -> Segment | None:
        for s in self.segments:
            if s.tag == tag and all(s.el(int(k[1:])) == v for k, v in where.items()):
                return s
        return None

    def groups(self, trigger: str, stop: set[str] | None = None) -> list[list[Segment]]:
        """Split the segments into groups, each starting at `trigger` (e.g. NAD, GID, EQD). A segment in `stop` closes
        the open group (used for nested groups)."""
        out: list[list[Segment]] = []
        cur: list[Segment] | None = None
        for s in self.segments:
            if s.tag == trigger:
                cur = [s]
                out.append(cur)
            elif cur is not None:
                if stop and s.tag in stop:
                    cur = None
                else:
                    cur.append(s)
        return out


@dataclass
class Interchange:
    syntax: str
    sender: str
    sender_qual: str
    recipient: str
    recipient_qual: str
    prepared: str
    control_ref: str
    test: bool
    messages: list[Message]
    delims: dict


# ---------------------------------------------------------------------------------------------------- lexing

def _tokenise(body: str, d: dict) -> list[list[list[str]]]:
    """Split into segments → elements → components, honouring the release character."""
    segs: list[list[list[str]]] = []
    seg: list[list[str]] = []
    el: list[str] = []
    buf: list[str] = []
    i, n = 0, len(body)
    rel, sep_c, sep_e, term = d["release"], d["component"], d["element"], d["segment"]
    while i < n:
        ch = body[i]
        if ch == rel and rel != " ":
            if i + 1 >= n:
                raise InterchangeError("The interchange ends with a release character.")
            buf.append(body[i + 1])
            i += 2
            continue
        if ch == sep_c:
            el.append("".join(buf)); buf = []
        elif ch == sep_e:
            el.append("".join(buf)); buf = []
            seg.append(el); el = []
        elif ch == term:
            el.append("".join(buf)); buf = []
            seg.append(el); el = []
            segs.append(seg); seg = []
        elif ch in "\r\n" and not buf and not el and not seg:
            pass  # line breaks between segments are tolerated (many partners send one segment per line)
        else:
            buf.append(ch)
        i += 1
    if buf or el or seg:
        tail = "".join(buf).strip()
        if tail or el or seg:
            raise InterchangeError("The last segment has no segment terminator.")
    return segs


def _to_segment(raw: list[list[str]]) -> Segment:
    tag = raw[0][0].strip() if raw and raw[0] else ""
    if not tag.isalnum() or len(tag) != 3:
        raise InterchangeError(f"Bad segment tag {tag!r}.")
    elements: list[Element] = []
    for comps in raw[1:]:
        elements.append(comps[0] if len(comps) == 1 else comps)
    while elements and elements[-1] in ("", []):
        elements.pop()
    return Segment(tag.upper(), elements)


def parse(raw: str | bytes) -> Interchange:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8") if not raw.startswith(b"\xef\xbb\xbf") else raw[3:].decode("utf-8")
    text = raw.lstrip("﻿").lstrip()
    d = dict(DEFAULT_DELIMS)
    if text.startswith("UNA"):
        una = text[3:9]
        if len(una) < 6:
            raise InterchangeError("UNA service string advice is shorter than 6 characters.")
        d = {"component": una[0], "element": una[1], "decimal": una[2], "release": una[3], "segment": una[5]}
        text = text[9:].lstrip("\r\n")
    segs = [_to_segment(s) for s in _tokenise(text, d)]
    if not segs or segs[0].tag != "UNB":
        raise InterchangeError("The interchange does not start with UNB.")
    unb = segs[0]
    syntax = unb.el(1, 1)
    if syntax and not syntax.startswith("UNO"):
        raise InterchangeError(f"Unsupported syntax identifier {syntax}.")
    unz = segs[-1]
    if unz.tag != "UNZ":
        raise InterchangeError("The interchange does not end with UNZ.")
    control_ref = unb.el(5)
    if unz.el(2) != control_ref:
        raise InterchangeError(f"UNZ control reference {unz.el(2)} does not match UNB {control_ref}.")
    messages: list[Message] = []
    cur: list[Segment] | None = None
    unh: Segment | None = None
    for s in segs[1:-1]:
        if s.tag == "UNH":
            if cur is not None:
                raise InterchangeError("UNH before the previous message's UNT.")
            cur, unh = [], s
        elif s.tag == "UNT":
            if cur is None or unh is None:
                raise InterchangeError("UNT without UNH.")
            count = len(cur) + 2
            if s.el(1) != str(count):
                raise InterchangeError(f"Message {unh.el(1)}: UNT says {s.el(1)} segments, the message has {count}.")
            if s.el(2) != unh.el(1):
                raise InterchangeError(f"UNT reference {s.el(2)} does not match UNH {unh.el(1)}.")
            messages.append(Message(ref=unh.el(1), type=unh.el(2, 1), version=unh.el(2, 2), release=unh.el(2, 3),
                                    agency=unh.el(2, 4), association=unh.el(2, 5), segments=cur))
            cur, unh = None, None
        elif s.tag in ("UNG", "UNE"):
            continue  # functional groups are allowed and not needed for routing here
        else:
            if cur is None:
                raise InterchangeError(f"Segment {s.tag} outside a message.")
            cur.append(s)
    if cur is not None:
        raise InterchangeError("A message has no UNT.")
    if unz.el(1) != str(len(messages)):
        raise InterchangeError(f"UNZ says {unz.el(1)} messages, the interchange has {len(messages)}.")
    return Interchange(syntax=syntax, sender=unb.el(2, 1), sender_qual=unb.el(2, 2), recipient=unb.el(3, 1),
                       recipient_qual=unb.el(3, 2), prepared=f"{unb.el(4, 1)}{unb.el(4, 2)}", control_ref=control_ref,
                       test=unb.el(11) == "1", messages=messages, delims=d)


# ---------------------------------------------------------------------------------------------------- writing

def escape(value: str, d: dict = DEFAULT_DELIMS) -> str:
    out = []
    for ch in str(value):
        if ch in (d["release"], d["component"], d["element"], d["segment"]):
            out.append(d["release"])
        out.append(ch)
    return "".join(out)


def clean(value, limit: int | None = None) -> str:
    """Text for an element: no line breaks, trimmed to the element's max length."""
    s = " ".join(str(value or "").split())
    return s[:limit] if limit else s


def segment_text(seg: Segment | tuple, d: dict = DEFAULT_DELIMS) -> str:
    tag, elements = (seg.tag, seg.elements) if isinstance(seg, Segment) else seg
    parts = [tag]
    for e in elements:
        if isinstance(e, (list, tuple)):
            comps = [escape(c or "", d) for c in e]
            while comps and comps[-1] == "":
                comps.pop()
            parts.append(d["component"].join(comps))
        else:
            parts.append(escape(e or "", d))
    while len(parts) > 1 and parts[-1] == "":
        parts.pop()
    return d["element"].join(parts) + d["segment"]


def write_interchange(sender: str, recipient: str, messages: list[tuple[str, str, str, list]], *, control_ref: str,
                      sender_qual: str = "ZZZ", recipient_qual: str = "ZZZ", test: bool = False,
                      when: datetime | None = None, newline: bool = True, msg_ref_start: int = 1) -> str:
    """messages: (type, version, release, segments) with segments as Segment or (tag, elements). Writes UNA, UNB,
    UNH/UNT per message with the right count, and UNZ."""
    when = when or datetime.now(timezone.utc)
    d = DEFAULT_DELIMS
    nl = "\n" if newline else ""
    out = ["UNA" + d["component"] + d["element"] + d["decimal"] + d["release"] + " " + d["segment"]]
    unb = ["UNB", [["UNOC", "4"], [sender, sender_qual], [recipient, recipient_qual], [when.strftime("%y%m%d"), when.strftime("%H%M")],
                   control_ref]]
    if test:
        unb[1] += ["", "", "", "", "", "1"]  # UNB 0035 test indicator (element 11)
    out.append(segment_text(tuple(unb), d))
    for i, (mtype, version, release, segs) in enumerate(messages):
        ref = str(msg_ref_start + i)
        body = [("UNH", [ref, [mtype, version, release, "UN"]])] + list(segs)
        out.extend(segment_text(s, d) for s in body)
        out.append(segment_text(("UNT", [str(len(body) + 1), ref]), d))
    out.append(segment_text(("UNZ", [str(len(messages)), control_ref]), d))
    return nl.join(out) + nl


def edi_datetime(iso: str | None, fmt: str = "203") -> str:
    """ISO 8601 → EDIFACT DTM value: 203 = CCYYMMDDHHMM, 102 = CCYYMMDD."""
    if not iso:
        return ""
    s = str(iso)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.fromisoformat(s[:10])
    return dt.strftime("%Y%m%d") if fmt == "102" else dt.strftime("%Y%m%d%H%M")


def parse_edi_datetime(value: str, fmt: str) -> str | None:
    """EDIFACT DTM value → ISO date or datetime (UTC assumed when no zone)."""
    v = (value or "").strip()
    try:
        if fmt == "102" and len(v) >= 8:
            return datetime.strptime(v[:8], "%Y%m%d").date().isoformat()
        if fmt in ("203", "204") and len(v) >= 12:
            return datetime.strptime(v[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc).isoformat()
        if fmt == "205" and len(v) >= 12:  # CCYYMMDDHHMMZZZ: we keep the local time as given
            return datetime.strptime(v[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None
    return None
