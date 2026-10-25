/**
 * Small form building blocks for the quote forms (V2).
 *
 * <Field label htmlFor error hint mark className>{control}</Field>
 *   label row (+ optional `mark`, e.g. <AgentMark/>), control, then error (role=alert) or hint under it.
 * <NumberInput value={number|null} onChange={(number|null)=>} integer min id invalid placeholder suffix aria-describedby />
 *   Text input with inputMode numeric/decimal. Accepts "1 234,5" and "1234.5". Empty is null, never 0.
 *   Keeps the raw text while typing; an unparseable entry shows invalid and reports null.
 */
import React, { useEffect, useState } from "react";
import { Input } from "@/components/ui/input";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function Field({ label, htmlFor, error, hint, mark, className, children, labelClassName }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      {label && (
        <div className="flex min-h-5 items-center gap-1.5">
          <label htmlFor={htmlFor} className={cn("text-xs text-ink-secondary", labelClassName)}>{label}</label>
          {mark}
        </div>
      )}
      {children}
      {error ? (
        <p id={htmlFor ? `${htmlFor}-err` : undefined} role="alert" className="text-xs text-danger-fg">{error}</p>
      ) : hint ? (
        <p className="text-xs text-ink-tertiary">{hint}</p>
      ) : null}
    </div>
  );
}

export function parseNumber(text, integer) {
  if (text === null || text === undefined) return null;
  const s = String(text).replace(/[\s  ]/g, "").replace(",", ".");
  if (s === "") return null;
  if (integer ? !/^-?\d+$/.test(s) : !/^-?\d*\.?\d+$|^-?\d+\.$/.test(s)) return NaN;
  const n = Number(s);
  return Number.isFinite(n) ? n : NaN;
}

function toText(v, locale) {
  if (v === null || v === undefined || Number.isNaN(v)) return "";
  const s = String(v);
  return locale === "fi" ? s.replace(".", ",") : s;
}

export function NumberInput({ value, onChange, integer, min = 0, invalid, className, suffix, ...rest }) {
  const { locale } = useT();
  const [text, setText] = useState(() => toText(value, locale));
  useEffect(() => {
    const parsed = parseNumber(text, integer);
    const same = (parsed === null && (value === null || value === undefined)) || parsed === value;
    if (!same && !(Number.isNaN(parsed) && (value === null || value === undefined))) setText(toText(value, locale));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  const parsed = parseNumber(text, integer);
  const bad = Number.isNaN(parsed) || (parsed !== null && min !== null && min !== undefined && parsed < min);
  const input = (
    <Input
      {...rest}
      type="text"
      inputMode={integer ? "numeric" : "decimal"}
      autoComplete="off"
      value={text}
      aria-invalid={invalid || bad ? "true" : undefined}
      className={cn("tabular", suffix && "pr-10", className)}
      onChange={(e) => {
        const t = e.target.value;
        setText(t);
        const n = parseNumber(t, integer);
        onChange(Number.isNaN(n) || (n !== null && min !== null && min !== undefined && n < min) ? null : n);
      }}
    />
  );
  if (!suffix) return input;
  return (
    <div className="relative">
      {input}
      <span className="pointer-events-none absolute inset-y-0 right-2.5 flex items-center text-xs text-ink-tertiary">{suffix}</span>
    </div>
  );
}
