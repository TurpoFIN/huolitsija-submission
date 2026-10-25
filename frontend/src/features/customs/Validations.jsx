// Validation checks: failing blocking checks first, grouped by who must act (customer / broker); passed ones collapsed.
import React, { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { OkGlyph, RuleChip } from "./common";

function Row({ v, onItem, actionFor }) {
  const { t } = useT();
  return (
    <li className="flex items-start gap-2 py-1.5">
      <OkGlyph ok={v.ok} severity={v.severity} />
      <span className="sr-only">{v.ok ? t("customs.val.passes") : t("customs.val.fails")}</span>
      <RuleChip rule={v.rule} />
      <span className={cn("min-w-0 flex-1 text-sm", v.ok ? "text-ink-secondary" : "text-foreground")}>{v.message}</span>
      {actionFor?.(v)}
      {v.item ? (
        onItem ? (
          <button type="button" onClick={() => onItem(v.item)} className="shrink-0 text-xs text-ink-accent hover:underline">
            {t("customs.val.item", { n: v.item })}
          </button>
        ) : (
          <span className="shrink-0 text-xs text-ink-tertiary">{t("customs.val.item", { n: v.item })}</span>
        )
      ) : null}
    </li>
  );
}

/**
 * validations: [{rule, ok, severity, owner, message, item, field}]
 * owners: which owner groups to show for failing checks (portal shows only "customer").
 * actionFor(v): optional node rendered next to a check (e.g. a link to where it is fixed).
 */
export default function Validations({ validations = [], onItem, actionFor, owners = ["customer", "broker"], showPassed = true }) {
  const { t } = useT();
  const [open, setOpen] = useState(false);
  if (!validations.length) return <p className="text-sm text-ink-tertiary">{t("customs.val.none")}</p>;
  const failing = validations.filter((v) => !v.ok && v.severity === "blocking");
  const advisory = validations.filter((v) => !v.ok && v.severity !== "blocking");
  const passed = validations.filter((v) => v.ok);
  const byOwner = owners.map((o) => ({ owner: o, rows: failing.filter((v) => (v.owner || "broker") === o) })).filter((g) => g.rows.length);

  return (
    <div className="flex flex-col gap-3">
      {failing.length === 0 ? (
        <p className="flex items-center gap-2 text-sm">
          <OkGlyph ok /> {t("customs.val.all_pass")}
        </p>
      ) : (
        byOwner.map((g) => (
          <div key={g.owner}>
            <p className="text-xs font-medium text-ink-tertiary">
              {t("customs.val.blocking")} · {t(`customs.val.owner.${g.owner}`)} <span className="tabular">({g.rows.length})</span>
            </p>
            <ul className="divide-y divide-[color:var(--border-subtle)]">
              {g.rows.map((v, i) => <Row key={`${v.rule}-${v.item}-${i}`} v={v} onItem={onItem} actionFor={actionFor} />)}
            </ul>
          </div>
        ))
      )}
      {advisory.length > 0 && (
        <div>
          <p className="text-xs font-medium text-ink-tertiary">{t("customs.val.advisory")} <span className="tabular">({advisory.length})</span></p>
          <ul className="divide-y divide-[color:var(--border-subtle)]">
            {advisory.map((v, i) => <Row key={`a-${v.rule}-${i}`} v={v} onItem={onItem} actionFor={actionFor} />)}
          </ul>
        </div>
      )}
      {showPassed && passed.length > 0 && (
        <div>
          <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
            className="inline-flex items-center gap-1 text-xs text-ink-secondary hover:text-foreground">
            {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
            {open ? t("customs.val.hide_passed") : t("customs.val.show_passed", { n: passed.length })}
          </button>
          {open && (
            <ul className="mt-1 divide-y divide-[color:var(--border-subtle)]">
              {passed.map((v, i) => <Row key={`p-${v.rule}-${v.item}-${i}`} v={v} onItem={onItem} />)}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
