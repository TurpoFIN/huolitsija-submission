/**
 * Accessible FAQ: native <details>/<summary> disclosures (keyboard + screen reader support, find-in-page works).
 * <Faq items={[{id, q, a}]} />
 */
import React from "react";
import { ChevronDown } from "lucide-react";

export default function Faq({ items }) {
  return (
    <div className="divide-y divide-[color:var(--border-subtle)] border-y border-[color:var(--border-subtle)]">
      {items.map((it) => (
        <details key={it.id} id={`faq-${it.id}`} className="group">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-4 py-3 text-md font-medium md:text-base [&::-webkit-details-marker]:hidden">
            <span>{it.q}</span>
            <ChevronDown className="size-4 shrink-0 text-ink-tertiary transition-transform group-open:rotate-180" aria-hidden />
          </summary>
          <p className="max-w-[72ch] pb-4 text-sm leading-6 text-ink-secondary">{it.a}</p>
        </details>
      ))}
    </div>
  );
}
