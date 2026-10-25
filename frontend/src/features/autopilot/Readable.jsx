import React from "react";
import { dateTime, money, num } from "@/lib/format";
import { useT } from "@/lib/i18n";

const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function scalar(ctx, k, v, key) {
  const { t, locale } = ctx;
  if (v === null || v === undefined || v === "") return <span key={key} className="text-ink-tertiary">—</span>;
  if (typeof v === "boolean") return <span key={key}>{t(v ? "ap.yes" : "ap.no")}</span>;
  if (typeof v === "number") {
    if (/_cents$/.test(k || "")) return <span key={key} className="tabular">{money(v, locale, { decimals: 2 })}</span>;
    return <span key={key} className="tabular">{num(v, locale, 3)}</span>;
  }
  const s = String(v);
  if (ISO.test(s)) return <span key={key} className="tabular">{dateTime(s, locale)}</span>;
  return <span key={key} className="whitespace-pre-wrap break-words">{s}</span>;
}

// A plain recursive function (not a self-rendering component): the dev-server's visual-edits Babel plugin overflows
// its stack on components that render themselves.
function render(ctx, value, depth, name) {
  if (value === null || value === undefined || typeof value !== "object") return scalar(ctx, name, value);
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-ink-tertiary">—</span>;
    if (value.every((x) => x === null || typeof x !== "object")) {
      return <span className="break-words">{value.flatMap((x, i) => (i > 0 ? [<span key={`c${i}`}>, </span>, scalar(ctx, name, x, i)] : [scalar(ctx, name, x, i)]))}</span>;
    }
    return (
      <ol className="flex flex-col gap-2">
        {value.slice(0, 50).map((x, i) => <li key={i} className="rounded-md border border-[color:var(--border-subtle)] p-2">{render(ctx, x, depth + 1, name)}</li>)}
      </ol>
    );
  }
  const entries = Object.entries(value);
  if (entries.length === 0) return <span className="text-ink-tertiary">—</span>;
  if (depth > 4) return <code className="break-all font-mono text-xs">{JSON.stringify(value)}</code>;
  return (
    <dl className="grid grid-cols-[minmax(7rem,max-content)_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
      {entries.flatMap(([k, v]) => [
        <dt key={`k-${k}`} className="pt-px font-mono text-xs text-ink-tertiary break-all">{k}</dt>,
        <dd key={`v-${k}`} className="min-w-0">{render(ctx, v, depth + 1, k)}</dd>,
      ])}
    </dl>
  );
}

/**
 * Agent inputs / decision payloads as a readable key list instead of raw JSON. Keys stay the API's field names (mono),
 * amounts in *_cents are shown in euros, ISO times as local time.
 */
export function Readable({ value }) {
  const ctx = useT();
  return render(ctx, value, 0, undefined);
}
