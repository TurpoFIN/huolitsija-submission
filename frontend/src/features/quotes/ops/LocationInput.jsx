// Compact location field for ops forms. Suggests from GET /api/public/locations (V2); the value is a
// Location object when a suggestion is chosen, else {query} which the backend resolves.
import React, { useEffect, useRef, useState } from "react";
import { MapPin } from "lucide-react";
import { apiGet } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useDebounced } from "./common";

export function LocationInput({ id, value, onChange, mode, placeholder, invalid }) {
  const text = value?.label || value?.name || value?.query || "";
  const [input, setInput] = useState(text);
  const [items, setItems] = useState([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const typed = useDebounced(input, 200);
  const wrap = useRef(null);
  const chosen = useRef(value?.unlocode ? value.label || value.name : null);

  useEffect(() => { setInput(value?.label || value?.name || value?.query || ""); }, [value]);

  useEffect(() => {
    let off = false;
    const q = typed.trim();
    if (q.length < 2 || q === chosen.current) { setItems([]); return undefined; }
    apiGet(`/public/locations?q=${encodeURIComponent(q)}${mode ? `&mode=${mode}` : ""}`)
      .then((r) => { if (!off) { setItems(r?.items || []); setActive(0); } })
      .catch(() => { if (!off) setItems([]); });
    return () => { off = true; };
  }, [typed, mode]);

  useEffect(() => {
    const close = (e) => { if (wrap.current && !wrap.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const choose = (loc) => { chosen.current = loc.label || loc.name; onChange(loc); setInput(loc.label || loc.name); setOpen(false); };

  return (
    <div ref={wrap} className="relative">
      <div className={cn("surface-input flex h-8 items-center gap-2 rounded-lg px-2.5", invalid && "shadow-[inset_0_0_0_1px_var(--danger-500)]")}>
        <MapPin className="size-4 shrink-0 text-ink-tertiary" aria-hidden />
        <input
          id={id}
          data-quiet-input
          role="combobox"
          aria-expanded={open && items.length > 0}
          aria-autocomplete="list"
          autoComplete="off"
          value={input}
          placeholder={placeholder}
          onChange={(e) => { setInput(e.target.value); setOpen(true); onChange(e.target.value.trim() ? { query: e.target.value } : null); }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (!open || items.length === 0) return;
            if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(items.length - 1, a + 1)); }
            if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
            if (e.key === "Enter") { e.preventDefault(); choose(items[active]); }
            if (e.key === "Escape") setOpen(false);
          }}
          className="min-w-0 flex-1 bg-transparent text-md outline-none placeholder:text-ink-tertiary md:text-sm"
        />
        {value?.unlocode && <span className="font-mono text-xs text-ink-tertiary">{value.unlocode}</span>}
      </div>
      {open && items.length > 0 && (
        <ul role="listbox" className="surface-overlay absolute z-50 mt-1 max-h-60 w-full overflow-y-auto rounded-lg bg-popover p-1 shadow-md">
          {items.map((l, i) => (
            <li key={`${l.unlocode}-${i}`} role="option" aria-selected={i === active}>
              <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => choose(l)}
                className={cn("flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-hover", i === active && "bg-hover")}>
                <span className="min-w-0 flex-1 truncate">{l.label || l.name}</span>
                <span className="font-mono text-xs text-ink-tertiary">{l.unlocode}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
