// Import declaration: duty basis per goods item. Decision first: does every item's duty come from TARIC (official
// measure, preference code DE 4/17, trade defence by additional code), or is any still an AI estimate / missing?
import React from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { pctFmt } from "@/features/customs/common";
import { bi, goodsCode } from "@/features/customs/extCommon";

/** The TARIC duty_for_item() result stored on the item by the backend hook, if any. */
export function taricOf(it) {
  return it?.taric || null;
}

export function dutySource(it) {
  if (it?.duty_rate_pct === null || it?.duty_rate_pct === undefined) return taricOf(it)?.status && taricOf(it).status !== "ok" ? "taric_open" : "none";
  return it.duty_source || "ai_estimate";
}

const SRC_TONE = { taric: "text-[color:var(--status-success-fg)]", broker: "text-ink-secondary", ai_estimate: "text-warning-fg",
  seed: "text-warning-fg", none: "text-warning-fg", taric_open: "text-warning-fg" };

function ItemRow({ it, decl }) {
  const { t, locale } = useT();
  const d = taricOf(it);
  const src = dutySource(it);
  const code = it.taric_code || it.cn_code;
  const q = new URLSearchParams({ code: String(code || "").replace(/\D/g, ""), country: it.origin_country || "", flow: "import" });
  return (
    <li className="py-3">
      <div className="grid gap-x-4 gap-y-1 sm:grid-cols-[2.5rem_minmax(0,1fr)_auto]">
        <span className="font-mono text-xs text-ink-tertiary">#{it.seq}</span>
        <div className="min-w-0">
          <p className="truncate text-sm">{it.description || "—"}</p>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-ink-secondary">
            <span className="font-mono">{goodsCode(code)}</span>
            <span>{t("cx.duty.origin")} <span className="font-mono">{it.origin_country || "—"}</span></span>
            <span>{t("cx.duty.preference")} <span className="font-mono">{it.preference || t("cx.missing")}</span></span>
            {d?.measure_type && <span>{t("cx.duty.measure")} <span className="font-mono">{d.measure_type}</span>{d.measure_type_text ? ` ${d.measure_type_text}` : ""}</span>}
            {d?.order_number && <span>{t("cx.taric.col.quota")} <span className="font-mono">{d.order_number}</span></span>}
            {code && <Link to={`/ops/customs/taric?${q}`} className="text-ink-accent hover:underline">{t("cx.duty.open_taric")}</Link>}
          </p>
          {d?.duty_expression && <p className="mt-0.5 text-xs text-ink-tertiary">{t("cx.duty.basis")} <span className="font-mono text-foreground">{d.duty_expression}</span>{d.regulation ? <> · <span className="font-mono">{d.regulation}</span></> : null}</p>}
          {(d?.trade_defence || []).map((x, i) => (
            <p key={`td-${i}`} className="mt-0.5 text-xs text-warning-fg">
              + {x.type || t("cx.taric.group.trade_defence")} <span className="font-mono">{x.duty_expression || pctFmt(x.rate_pct, locale)}</span>
              {x.additional_code && <> · {t("cx.duty.addcode")} <span className="font-mono">{x.additional_code}</span>{x.additional_code_assumed_residual ? ` (${t("cx.duty.residual")})` : ""}</>}
            </p>
          ))}
          {(d?.additional_duties || []).map((x, i) => (
            <p key={`ad-${i}`} className="mt-0.5 text-xs text-ink-secondary">+ {x.type} <span className="font-mono">{x.duty_expression}</span></p>
          ))}
          {(d?.reasons || []).map((r, i) => <p key={`r-${i}`} className="mt-0.5 text-xs text-warning-fg">{bi(locale, r)}</p>)}
        </div>
        <div className="flex flex-col items-start sm:items-end">
          <span className="tabular text-sm">{it.duty_cents == null ? t("cx.missing") : money(it.duty_cents, locale, { decimals: 2 })}</span>
          <span className={cn("text-[11px] whitespace-nowrap", SRC_TONE[src])}>
            {it.duty_rate_pct !== null && it.duty_rate_pct !== undefined ? `${pctFmt(it.duty_rate_pct, locale)} · ` : ""}{t(`cx.duty.src.${src}`)}
          </span>
        </div>
      </div>
    </li>
  );
}

export default function TaricDutyBasis({ decl }) {
  const { t } = useT();
  const items = decl?.items || [];
  if (!items.length) return null;
  const notTaric = items.filter((it) => dutySource(it) !== "taric");
  return (
    <div>
      <p className={cn("mb-1 inline-flex items-center gap-1.5 text-sm", notTaric.length ? "text-warning-fg" : "text-ink-secondary")}>
        {notTaric.length ? <AlertTriangle className="size-4" aria-hidden /> : <CheckCircle2 className="size-4 text-[color:var(--status-success-fg)]" aria-hidden />}
        {notTaric.length ? t("cx.duty.estimate_n", { n: notTaric.length }) : t("cx.duty.all_taric")}
      </p>
      <ul className="divide-y divide-[color:var(--border-subtle)]">
        {items.map((it) => <ItemRow key={it.seq} it={it} decl={decl} />)}
      </ul>
    </div>
  );
}
