import React from "react";
import { EmptyState, Loadable } from "@/components/app/states";
import { useT } from "@/lib/i18n";
import { legalBasis, pick } from "./shared";

/** Lakisääteiset kohdat: where an agent never decides alone, whatever Autopilot says, and the rule behind each. */
export function LegalPoints({ q }) {
  const { t, locale } = useT();
  return (
    <div className="max-w-[1024px]">
      <p className="mb-3 max-w-[72ch] text-sm text-ink-secondary">{t("ap.legal.intro")}</p>
      <Loadable q={q} isEmpty={(d) => !d.items.length} empty={<EmptyState title={t("ap.legal.empty")} />}>
        {(d) => (
          <ol className="divide-y divide-[color:var(--border-subtle)] border-y border-[color:var(--border-subtle)]">
            {d.items.map((p) => (
              <li key={p.code} className="py-3">
                <h3 className="text-sm font-medium">{pick(locale, p.title_fi, p.title_en)}</h3>
                <p className="mt-1 max-w-[80ch] text-sm text-ink-secondary">{pick(locale, p.reason_fi, p.reason_en)}</p>
                <p className="mt-1 max-w-[80ch] text-xs text-ink-tertiary"><span className="text-ink-secondary">{t("ap.legal.basis")}:</span> {legalBasis(locale, p.basis)}</p>
              </li>
            ))}
          </ol>
        )}
      </Loadable>
    </div>
  );
}
