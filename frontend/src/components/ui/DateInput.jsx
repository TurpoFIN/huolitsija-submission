import * as React from "react";
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";
import { format, isValid, parse, parseISO, startOfDay } from "date-fns";
import { enGB, fi } from "date-fns/locale";

import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/*
 * Locale-aware date field: a text input in the UI locale's format plus a Monday-first calendar popover.
 *   fi -> pp.kk.vvvv (26.10.2026)     en -> dd/mm/yyyy (en-GB)
 * The value in and out is always an ISO date string "yyyy-mm-dd" (or "" when empty), so it drops in where
 * <Input type="date"> was used: <DateInput value={iso} onChange={(iso) => ...} min={todayIso} />.
 * Typed input is parsed on blur / Enter; short forms work ("26.10.", "26.10.26", "26/10"), and ISO is accepted too.
 * Keyboard: ArrowDown (with or without Alt) opens the calendar (arrow keys move, Enter picks, Esc closes).
 */

const LOCALES = {
  fi: {
    dateFns: fi,
    display: "dd.MM.yyyy",
    placeholder: "pp.kk.vvvv",
    parse: ["d.M.yyyy", "d.M.yy", "d.M.", "d.M", "ddMMyyyy", "ddMMyy", "yyyy-MM-dd"],
    open: "Avaa kalenteri",
    today: "Tänään",
    clear: "Tyhjennä",
    invalid: "Tarkista päivämäärä (pp.kk.vvvv)",
    range: "Päivä on sallitun aikavälin ulkopuolella",
  },
  en: {
    dateFns: enGB,
    display: "dd/MM/yyyy",
    placeholder: "dd/mm/yyyy",
    parse: ["d/M/yyyy", "d/M/yy", "d/M", "d.M.yyyy", "d-M-yyyy", "ddMMyyyy", "yyyy-MM-dd"],
    open: "Open calendar",
    today: "Today",
    clear: "Clear",
    invalid: "Check the date (dd/mm/yyyy)",
    range: "Date is outside the allowed range",
  },
};

function fromIso(iso) {
  if (!iso) return null;
  const d = parseISO(String(iso).slice(0, 10));
  return isValid(d) ? startOfDay(d) : null;
}

function toIso(d) {
  return d ? format(d, "yyyy-MM-dd") : "";
}

function parseTyped(text, cfg) {
  const s = (text || "").trim().replace(/\s+/g, "");
  if (!s) return null;
  const ref = startOfDay(new Date());
  for (const f of cfg.parse) {
    const d = parse(s, f, ref);
    // reject partial matches ("26.10.2026abc") and absurd years
    if (isValid(d) && d.getFullYear() >= 1900 && d.getFullYear() <= 2200) {
      if (!/y/.test(f) && d < ref) {
        // "26.10." means the next such date, not one in the past
        const next = new Date(d);
        next.setFullYear(d.getFullYear() + 1);
        return startOfDay(next);
      }
      return startOfDay(d);
    }
  }
  return null;
}

/*
 * yearNavigation: month and year dropdowns in the calendar caption, for dates far from today (a birth date).
 * The years run from min (default 120 years back) to max (default 10 years ahead).
 */
function yearNav(minDate, maxDate, today) {
  return {
    captionLayout: "dropdown",
    fromYear: minDate ? minDate.getFullYear() : today.getFullYear() - 120,
    toYear: maxDate ? maxDate.getFullYear() : today.getFullYear() + 10,
    classNames: {
      caption_label: "hidden",
      caption_dropdowns: "flex items-center gap-1",
      dropdown_month: "relative inline-flex items-center",
      dropdown_year: "relative inline-flex items-center",
      // native select arrow: the day-picker draws its own arrow inside the caption label, which is hidden here
      dropdown:
        "h-7 cursor-pointer rounded-md border border-input bg-background py-0 pl-1.5 pr-1 text-sm font-medium " +
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      vhidden: "sr-only",
    },
    components: {
      // Calendar's own components prop is replaced by this one, so its arrow icons are repeated here
      IconLeft: ({ className, ...p }) => <ChevronLeft className={cn("h-4 w-4", className)} {...p} />,
      IconRight: ({ className, ...p }) => <ChevronRight className={cn("h-4 w-4", className)} {...p} />,
    },
  };
}

const DateInput = React.forwardRef(function DateInput(
  {
    value,
    onChange,
    min,
    max,
    id,
    name,
    className,
    inputClassName,
    placeholder,
    disabled,
    required,
    autoFocus,
    onBlur,
    clearable = false,
    yearNavigation = false,
    "aria-invalid": ariaInvalid,
    "aria-label": ariaLabel,
    "aria-describedby": ariaDescribedBy,
    "data-testid": testId,
  },
  ref
) {
  const { locale } = useT();
  const cfg = LOCALES[locale] || LOCALES.en;
  const selected = fromIso(value);
  const minDate = fromIso(min);
  const maxDate = fromIso(max);
  const [text, setText] = React.useState(selected ? format(selected, cfg.display) : "");
  const [open, setOpen] = React.useState(false);
  const [error, setError] = React.useState(null);
  const inputRef = React.useRef(null);
  React.useImperativeHandle(ref, () => inputRef.current);
  const errId = React.useId();

  // keep the text in sync when the value or the locale changes from outside
  React.useEffect(() => {
    setText(selected ? format(selected, cfg.display) : "");
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, locale]);

  const outOfRange = (d) => (minDate && d < minDate) || (maxDate && d > maxDate);

  const commit = (raw) => {
    const s = (raw ?? text).trim();
    if (!s) {
      setError(null);
      if (value) onChange?.("");
      return;
    }
    const d = parseTyped(s, cfg);
    if (!d) {
      setError(cfg.invalid);
      return;
    }
    if (outOfRange(d)) {
      setError(cfg.range);
      return;
    }
    setError(null);
    setText(format(d, cfg.display));
    if (toIso(d) !== (value || "")) onChange?.(toIso(d));
  };

  const pick = (d) => {
    if (!d) return;
    setError(null);
    setText(format(d, cfg.display));
    onChange?.(toIso(d));
    setOpen(false);
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const today = startOfDay(new Date());
  const todayAllowed = !outOfRange(today);
  const invalid = Boolean(error) || ariaInvalid === true || ariaInvalid === "true";
  const disabledDays = [minDate && { before: minDate }, maxDate && { after: maxDate }].filter(Boolean);

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
            autoFocus={autoFocus}
            data-slot="input"
            data-testid={testId}
            disabled={disabled}
            required={required}
            placeholder={placeholder || cfg.placeholder}
            value={text}
            aria-label={ariaLabel}
            aria-invalid={invalid ? "true" : undefined}
            aria-describedby={[ariaDescribedBy, error ? errId : null].filter(Boolean).join(" ") || undefined}
            title={error || undefined}
            onChange={(e) => {
              setText(e.target.value);
              if (error) setError(null);
            }}
            onBlur={(e) => {
              commit(e.target.value);
              onBlur?.(e);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commit(e.currentTarget.value);
              } else if (e.key === "ArrowDown") {
                e.preventDefault();
                setOpen(true);
              } else if (e.key === "Escape" && open) {
                setOpen(false);
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
              aria-label={cfg.open}
              aria-haspopup="dialog"
              aria-expanded={open}
              className="absolute right-1 top-1/2 inline-flex size-6 -translate-y-1/2 items-center justify-center rounded-md text-ink-secondary hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            >
              <CalendarDays className="size-4" aria-hidden />
            </button>
          </PopoverTrigger>
          {error ? (
            <span id={errId} role="alert" className="sr-only">
              {error}
            </span>
          ) : null}
        </div>
      </PopoverAnchor>
      <PopoverContent
        align="start"
        className="w-auto p-0"
        onOpenAutoFocus={(e) => {
          // let DayPicker focus the selected (or first allowed) day for keyboard use
          e.preventDefault();
        }}
      >
        <Calendar
          mode="single"
          locale={cfg.dateFns}
          weekStartsOn={1}
          selected={selected || undefined}
          onSelect={pick}
          defaultMonth={selected || (minDate && minDate > today ? minDate : today)}
          fromDate={minDate || undefined}
          toDate={maxDate || undefined}
          disabled={disabledDays}
          initialFocus
          {...(yearNavigation ? yearNav(minDate, maxDate, today) : {})}
        />
        {(todayAllowed || clearable) && (
          <div className="flex items-center justify-between gap-2 border-t px-3 py-2">
            {todayAllowed ? (
              <button type="button" className="text-sm text-primary hover:underline" onClick={() => pick(today)}>
                {cfg.today}
              </button>
            ) : (
              <span />
            )}
            {clearable && value ? (
              <button
                type="button"
                className="text-sm text-ink-secondary hover:underline"
                onClick={() => {
                  setText("");
                  onChange?.("");
                  setOpen(false);
                }}
              >
                {cfg.clear}
              </button>
            ) : null}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
});

export { DateInput };
export default DateInput;
