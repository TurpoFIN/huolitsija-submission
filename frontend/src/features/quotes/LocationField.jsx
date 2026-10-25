/**
 * LocationField — debounced place autocomplete (ARIA combobox).
 *
 * Props (stable, used by portal, public site and ops):
 *   value        Location | {query, label} | null   Location = {unlocode, name, country, lat, lon, kinds[], label}
 *   onChange     (Location | {query, label} | null) => void
 *                - picking a suggestion sends the full Location
 *                - leaving the field with unmatched text sends {query, label} (the backend resolves it)
 *                - clearing / starting to type again sends null
 *   label        visible label (optional; without it `placeholder` becomes the accessible name)
 *   placeholder  input placeholder
 *   id           input id (for external <label htmlFor> / error ids)
 *   invalid      bool, red edge + aria-invalid
 *   mode         "sea"|"road"|"air"|"rail"|null, passed to the search as &mode=
 *   source       "auto" (GET /api/locations, on any error GET /api/public/locations) | "public" (only the public endpoint)
 *   mark         node shown next to the label (e.g. <AgentMark/> when AI filled it)
 *   disabled, autoFocus, className, "aria-describedby"
 * Keyboard: ArrowDown/ArrowUp move, Enter picks, Escape closes (and restores the picked value), Tab leaves.
 */
import React, { useEffect, useId, useRef, useState } from "react";
import { Anchor, Loader2, MapPin, Plane, TrainFront, Truck, X } from "lucide-react";
import { apiGet } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { countryName, normalizeLocation } from "./quoteFormat";

const KIND_ICONS = { port: Anchor, airport: Plane, rail: TrainFront, road: Truck, city: MapPin };
let v1MissingUntil = 0; // /api/locations returned 404: skip it for a minute

async function searchLocations(q, { mode, source, signal }) {
  const qs = `q=${encodeURIComponent(q)}${mode ? `&mode=${encodeURIComponent(mode)}` : ""}`;
  const norm = (r) => (Array.isArray(r) ? r : r?.items || []).map(normalizeLocation).filter(Boolean).slice(0, 10);
  if (source !== "public" && Date.now() > v1MissingUntil) {
    try {
      return norm(await apiGet(`/locations?${qs}`, { signal }));
    } catch (e) {
      if (e.name === "AbortError") throw e;
      if (e.status === 404) v1MissingUntil = Date.now() + 60000;
    }
  }
  return norm(await apiGet(`/public/locations?${qs}`, { signal }));
}

function displayText(v) {
  if (!v) return "";
  return v.unlocode ? v.name || v.label || "" : v.label || v.query || v.name || "";
}

export default function LocationField({
  value, onChange, label, placeholder, id, invalid, mode, source = "auto", mark, disabled, autoFocus, className,
  "aria-describedby": describedBy,
}) {
  const { t, locale } = useT();
  const auto = useId().replace(/:/g, "");
  const inputId = id || `loc-${auto}`;
  const listId = `${inputId}-list`;
  const [text, setText] = useState(() => displayText(value));
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState([]);
  const [active, setActive] = useState(-1);
  const [status, setStatus] = useState("idle"); // idle | loading | done | error
  const editing = useRef(false);
  const listRef = useRef(null);

  useEffect(() => {
    if (!editing.current) setText(displayText(value));
  }, [value]);

  const q = text.trim();
  useEffect(() => {
    if (!open || !editing.current || q.length < 2) {
      setItems([]);
      setStatus("idle");
      return undefined;
    }
    const ctrl = new AbortController();
    setStatus("loading");
    const h = setTimeout(async () => {
      try {
        const res = await searchLocations(q, { mode, source, signal: ctrl.signal });
        setItems(res);
        setActive(res.length ? 0 : -1);
        setStatus("done");
      } catch (e) {
        if (e.name === "AbortError") return;
        setItems([]);
        setActive(-1);
        setStatus("error");
      }
    }, 220);
    return () => {
      clearTimeout(h);
      ctrl.abort();
    };
  }, [q, open, mode, source]);

  useEffect(() => {
    if (active < 0 || !listRef.current) return;
    const el = listRef.current.querySelector(`[data-idx="${active}"]`);
    el?.scrollIntoView?.({ block: "nearest" });
  }, [active]);

  const pick = (loc) => {
    editing.current = false;
    setText(displayText(loc));
    setOpen(false);
    setItems([]);
    onChange(loc);
  };

  const commitFreeText = () => {
    editing.current = false;
    setOpen(false);
    if (!q) {
      if (value) onChange(null);
      return;
    }
    if (value && displayText(value) === text) return;
    onChange({ query: q, label: q });
  };

  const onKeyDown = (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) setOpen(true);
      setActive((a) => (items.length ? (a + 1) % items.length : -1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => (items.length ? (a <= 0 ? items.length - 1 : a - 1) : -1));
    } else if (e.key === "Enter") {
      if (open && active >= 0 && items[active]) {
        e.preventDefault();
        pick(items[active]);
      } else if (open) {
        e.preventDefault();
        commitFreeText();
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
        if (value) {
          editing.current = false;
          setText(displayText(value));
        }
      }
    }
  };

  const resolved = value && (value.unlocode || (Number.isFinite(value.lat) && Number.isFinite(value.lon))) && !editing.current && displayText(value) === text;
  const unmatched = value && !resolved && (value.query || value.label) && !open;
  const showList = open && editing.current && q.length >= 2;

  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      {label && (
        <div className="flex min-h-5 items-center gap-1.5">
          <label htmlFor={inputId} className="text-xs text-ink-secondary">{label}</label>
          {mark}
        </div>
      )}
      <div className="relative">
        <MapPin className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-tertiary" aria-hidden />
        <input
          id={inputId}
          type="text"
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={showList}
          aria-controls={listId}
          aria-activedescendant={showList && active >= 0 ? `${listId}-${active}` : undefined}
          aria-invalid={invalid ? "true" : undefined}
          aria-label={label ? undefined : placeholder}
          aria-describedby={describedBy}
          autoComplete="off"
          spellCheck={false}
          disabled={disabled}
          autoFocus={autoFocus}
          placeholder={placeholder}
          value={text}
          onChange={(e) => {
            editing.current = true;
            setText(e.target.value);
            setOpen(true);
            if (value) onChange(null);
          }}
          onFocus={() => {
            if (editing.current) setOpen(true);
          }}
          onBlur={() => {
            if (editing.current) commitFreeText();
            else setOpen(false);
          }}
          onKeyDown={onKeyDown}
          className={cn(
            "surface-input flex h-8 w-full min-w-0 rounded-lg bg-transparent pl-8 text-md md:text-sm placeholder:text-ink-tertiary disabled:cursor-not-allowed disabled:opacity-50",
            resolved || unmatched || text ? "pr-[4.5rem]" : "pr-2.5"
          )}
        />
        <div className="absolute inset-y-0 right-1.5 flex items-center gap-1">
          {status === "loading" && showList && <Loader2 className="size-3.5 animate-spin text-ink-tertiary" aria-hidden />}
          {resolved && <span className="font-mono text-[11px] text-ink-tertiary">{value.unlocode || value.country}</span>}
          {unmatched && <span className="text-[11px] text-ink-tertiary" title={t("quotes.loc.unmatched_hint")}>{t("quotes.loc.unmatched")}</span>}
          {text && !disabled && (
            <button
              type="button"
              tabIndex={-1}
              aria-label={t("action.clear")}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                editing.current = false;
                setText("");
                setOpen(false);
                onChange(null);
              }}
              className="rounded-full p-0.5 text-ink-tertiary hover:bg-hover hover:text-foreground"
            >
              <X className="size-3.5" />
            </button>
          )}
        </div>
        {showList && (
          <div className="surface-overlay absolute left-0 top-full z-50 mt-1 w-full min-w-[260px] overflow-hidden rounded-lg bg-popover">
            <ul ref={listRef} id={listId} role="listbox" aria-label={label || placeholder} className="max-h-72 overflow-y-auto p-1">
              {items.map((loc, i) => (
                <li
                  key={`${loc.unlocode || loc.name}-${i}`}
                  id={`${listId}-${i}`}
                  data-idx={i}
                  role="option"
                  aria-selected={i === active}
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => pick(loc)}
                  className={cn("flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5", i === active && "bg-hover")}
                >
                  <span className="flex w-9 shrink-0 items-center gap-0.5 text-ink-tertiary" aria-hidden>
                    {(loc.kinds || []).slice(0, 2).map((k) => {
                      const I = KIND_ICONS[k] || MapPin;
                      return <I key={k} className="size-3.5" />;
                    })}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm">{loc.name}</span>
                    <span className="block truncate text-xs text-ink-tertiary">
                      {[countryName(loc.country, locale), (loc.kinds || []).map((k) => t(`quotes.loc.kind.${k}`)).join(", ")].filter(Boolean).join(" · ")}
                    </span>
                  </span>
                  {loc.unlocode && <span className="shrink-0 font-mono text-xs text-ink-secondary">{loc.unlocode}</span>}
                </li>
              ))}
            </ul>
            {status === "loading" && items.length === 0 && <p className="px-3 py-2 text-xs text-ink-tertiary">{t("quotes.loc.searching")}</p>}
            {status === "done" && items.length === 0 && <p className="px-3 py-2 text-xs text-ink-tertiary">{t("quotes.loc.none", { q })}</p>}
            {status === "error" && <p className="px-3 py-2 text-xs text-ink-tertiary">{t("quotes.loc.error")}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
