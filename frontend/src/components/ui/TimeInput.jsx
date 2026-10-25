import * as React from "react";
import { Clock } from "lucide-react";

import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/*
 * 24-hour time field that looks the same in every browser (the native <input type="time"> shows AM/PM in a US-locale
 * browser). Value in and out is "HH:MM" (or ""), so it drops in where type="time" was used:
 *   <TimeInput value={hhmm} onChange={(hhmm) => ...} step={15} />
 * Display: fi "08.30" (Finnish notation), en "08:30". Typing accepts 8, 830, 0830, 8.30, 8:30, 8,30 (not 8.3).
 * Keyboard: ArrowUp / ArrowDown move by `step` minutes; Enter commits; the clock button lists common slots.
 */

const TEXT = {
  fi: { sep: ".", placeholder: "hh.mm", open: "Valitse kellonaika", invalid: "Tarkista kellonaika (hh.mm)" },
  en: { sep: ":", placeholder: "hh:mm", open: "Choose a time", invalid: "Check the time (hh:mm)" },
};

const pad = (n) => String(n).padStart(2, "0");
const toMin = (hhmm) => {
  const m = /^(\d{2}):(\d{2})$/.exec(hhmm || "");
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
};
const fromMin = (min) => `${pad(Math.floor(min / 60))}:${pad(min % 60)}`;

function parseTyped(text) {
  const s = (text || "").trim().replace(/\s+/g, "");
  if (!s) return "";
  let h;
  let m;
  // minutes need two digits after a separator: "7.5" could mean 7.05 or 7.30, so it is refused rather than guessed
  const sep = /^(\d{1,2})[.:,](\d{2})$/.exec(s);
  if (sep) {
    h = Number(sep[1]);
    m = Number(sep[2]);
  } else if (/^\d{1,4}$/.test(s)) {
    if (s.length <= 2) {
      h = Number(s);
      m = 0;
    } else {
      h = Number(s.slice(0, s.length - 2));
      m = Number(s.slice(-2));
    }
  } else {
    return null;
  }
  if (h > 23 || m > 59) return null;
  return `${pad(h)}:${pad(m)}`;
}

const TimeInput = React.forwardRef(function TimeInput(
  {
    value,
    onChange,
    id,
    name,
    className,
    inputClassName,
    placeholder,
    disabled,
    required,
    step = 15,
    slotFrom = 6 * 60,
    slotTo = 20 * 60,
    slotStep = 30,
    onBlur,
    "aria-invalid": ariaInvalid,
    "aria-label": ariaLabel,
    "aria-describedby": ariaDescribedBy,
  },
  ref
) {
  const { locale } = useT();
  const txt = TEXT[locale] || TEXT.en;
  const show = React.useCallback((hhmm) => (hhmm ? hhmm.replace(":", txt.sep) : ""), [txt.sep]);
  const [text, setText] = React.useState(show(value));
  const [open, setOpen] = React.useState(false);
  const [error, setError] = React.useState(false);
  const inputRef = React.useRef(null);
  const listRef = React.useRef(null);
  React.useImperativeHandle(ref, () => inputRef.current);

  React.useEffect(() => {
    setText(show(value));
    setError(false);
  }, [value, show]);

  const commit = (raw) => {
    const parsed = parseTyped(raw ?? text);
    if (parsed === null) {
      setError(true);
      return;
    }
    setError(false);
    setText(show(parsed));
    if (parsed !== (value || "")) onChange?.(parsed);
  };

  const nudge = (dir) => {
    const cur = toMin(parseTyped(text) || value) ?? 8 * 60;
    const next = (((Math.round(cur / step) * step + dir * step) % 1440) + 1440) % 1440;
    setError(false);
    setText(show(fromMin(next)));
    onChange?.(fromMin(next));
  };

  const slots = [];
  for (let t = slotFrom; t <= slotTo; t += slotStep) slots.push(fromMin(t));

  // scroll the current (or nearest) slot into view when the list opens
  React.useEffect(() => {
    if (!open) return;
    requestAnimationFrame(() => {
      const el = listRef.current?.querySelector('[aria-selected="true"]') || listRef.current?.querySelector('[data-near="true"]');
      el?.scrollIntoView({ block: "center" });
      el?.focus();
    });
  }, [open]);

  const cur = toMin(value);
  const nearest = cur === null ? "08:00" : slots.reduce((a, b) => (Math.abs(toMin(b) - cur) < Math.abs(toMin(a) - cur) ? b : a), slots[0]);
  const invalid = error || ariaInvalid === true || ariaInvalid === "true";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <div className={cn("relative w-full min-w-0", className)}>
          <input
            ref={inputRef}
            id={id}
            name={name}
            type="text"
            inputMode="numeric"
            autoComplete="off"
            data-slot="input"
            disabled={disabled}
            required={required}
            placeholder={placeholder || txt.placeholder}
            value={text}
            aria-label={ariaLabel}
            aria-invalid={invalid ? "true" : undefined}
            aria-describedby={ariaDescribedBy}
            title={error ? txt.invalid : undefined}
            onChange={(e) => {
              setText(e.target.value);
              if (error) setError(false);
            }}
            onBlur={(e) => {
              commit(e.target.value);
              onBlur?.(e);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commit(e.currentTarget.value);
              } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
                e.preventDefault();
                nudge(e.key === "ArrowUp" ? 1 : -1);
              }
            }}
            className={cn(
              "surface-input flex h-8 w-full min-w-0 rounded-lg bg-transparent py-1 pl-2.5 pr-8 text-md md:text-sm tabular-nums transition-[box-shadow] placeholder:text-ink-tertiary disabled:cursor-not-allowed disabled:opacity-50",
              inputClassName
            )}
          />
          <PopoverTrigger asChild>
            <button
              type="button"
              disabled={disabled}
              aria-label={txt.open}
              aria-haspopup="listbox"
              aria-expanded={open}
              className="absolute right-1 top-1/2 inline-flex size-6 -translate-y-1/2 items-center justify-center rounded-md text-ink-secondary hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            >
              <Clock className="size-4" aria-hidden />
            </button>
          </PopoverTrigger>
        </div>
      </PopoverAnchor>
      <PopoverContent align="start" className="w-28 p-1" onOpenAutoFocus={(e) => e.preventDefault()}>
        <div
          ref={listRef}
          role="listbox"
          aria-label={txt.open}
          className="max-h-60 overflow-y-auto"
          onKeyDown={(e) => {
            const items = Array.from(listRef.current?.querySelectorAll('[role="option"]') || []);
            const i = items.indexOf(document.activeElement);
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              items[Math.min(items.length - 1, Math.max(0, i + (e.key === "ArrowDown" ? 1 : -1)))]?.focus();
            }
          }}
        >
          {slots.map((s) => (
            <button
              key={s}
              type="button"
              role="option"
              aria-selected={s === value}
              data-near={s === nearest ? "true" : undefined}
              className={cn(
                "flex w-full items-center rounded-md px-2 py-1.5 text-left text-sm tabular-nums hover:bg-accent focus-visible:bg-accent focus-visible:outline-none",
                s === value && "bg-primary text-primary-foreground hover:bg-primary"
              )}
              onClick={() => {
                setError(false);
                setText(show(s));
                onChange?.(s);
                setOpen(false);
                requestAnimationFrame(() => inputRef.current?.focus());
              }}
            >
              {show(s)}
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
});

export { TimeInput };
export default TimeInput;
