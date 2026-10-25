// Sanctions screening evidence: hit list, list versions, and an ad-hoc name check (ops only).
import React, { useState } from "react";
import { Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState } from "@/components/app/states";
import { apiPost } from "@/lib/api";
import { date, num, pct, relTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const SHORT = { eu: "EU", un: "UN", ofac: "OFAC", uk: "UK", ofsi: "OFSI", csl: "US CSL" };
const scorePct = (s, locale) => pct(Math.round((s || 0) * 1000) / 10, locale, 0);

/** One line per list: "EU · 23 Oct" — which list versions a screening ran against. */
export function ListVersions({ lists, prefix, className }) {
  const { locale } = useT();
  if (!lists?.length) return null;
  return (
    <span className={cn("text-xs text-ink-tertiary", className)}>
      {prefix ? `${prefix}: ` : ""}
      {lists.map((l) => `${SHORT[l.list] || l.list} ${l.loaded === false || l.status === "error" ? "—" : date(l.generated_at || l.fetched_at, locale, { day: "numeric", month: "short" })}`).join(" · ")}
    </span>
  );
}

export function ScreeningHits({ hits, className }) {
  const { t, locale } = useT();
  if (!hits?.length) return null;
  return (
    <ul className={cn("flex flex-col divide-y divide-[color:var(--border-subtle)] rounded-lg border", className)}>
      {hits.map((h, i) => (
        <li key={`${h.list}-${h.ref}-${i}`} className="flex flex-col gap-1 px-3 py-2">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <p className="text-sm font-medium break-words">{h.matched_name}</p>
            <span className="flex items-center gap-2 text-xs tabular text-ink-secondary">
              {h.previously_cleared && <StatusBadge size="sm" state="expired" label={t("customer.hits.cleared_earlier")} />}
              {t("customer.hits.score", { score: scorePct(h.score, locale) })}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-ink-tertiary">
            <Badge variant="outline">{SHORT[h.list] || h.list}</Badge>
            {h.ref && <span className="font-mono">{t("customer.hits.ref", { ref: h.ref })}</span>}
            {h.type && <span>· {h.type}</span>}
            {h.countries?.length > 0 && <span>· {h.countries.join(", ")}</span>}
            {h.primary_name && h.primary_name !== h.matched_name && <span>· {h.primary_name}</span>}
          </div>
          {h.programmes?.length > 0 && (
            <p className="text-xs text-ink-tertiary">{t("customer.hits.programmes")}: {h.programmes.join(", ")}</p>
          )}
          {h.query && <p className="text-xs text-ink-tertiary">{t("customer.hits.role", { name: h.role ? `${h.query} (${h.role})` : h.query })}</p>}
          {h.explanation && <p className="text-xs text-ink-secondary">{h.explanation}</p>}
        </li>
      ))}
    </ul>
  );
}

/** List freshness table: which version of each list screening uses right now. */
export function ScreeningLists({ lists }) {
  const { t, locale } = useT();
  if (!lists?.length) return null;
  return (
    <ul className="flex flex-col gap-1.5">
      {lists.map((l) => {
        const ok = l.loaded !== false && l.status !== "error";
        return (
          <li key={l.list} className="flex flex-wrap items-baseline justify-between gap-x-3 text-sm">
            <a href={l.source_url} target="_blank" rel="noreferrer" className="min-w-0 hover:underline">{l.label || l.list}</a>
            {ok ? (
              <span className="text-xs tabular text-ink-tertiary">
                {t("customer.lists.entries", { n: num(l.entries, locale) })} · {t("customer.lists.published", { date: date(l.generated_at, locale) })} · {t("customer.lists.fetched", { time: relTime(l.fetched_at, locale) })}
              </span>
            ) : (
              <StatusBadge size="sm" state="failed" label={t("customer.lists.not_loaded")} />
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** Ad-hoc screening of any name against the cached EU, UN and OFAC lists. */
export function ScreeningCheck() {
  const { t, locale } = useT();
  const [name, setName] = useState("");
  const [kind, setKind] = useState("any");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const run = async (e) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      setRes(await apiPost("/screening/check", { name: name.trim(), kind }));
    } catch (ex) {
      setErr(ex);
    } finally {
      setBusy(false);
    }
  };
  const lists = (res?.lists || []).map((l) => SHORT[l.list] || l.list).join(", ");
  return (
    <div className="flex flex-col gap-2">
      <form onSubmit={run} className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("customer.screen.placeholder")}
          aria-label={t("customer.screen.placeholder")} className="sm:flex-1" />
        <Segmented value={kind} onChange={setKind} label={t("customer.screen.title")}
          options={["any", "entity", "person"].map((k) => ({ value: k, label: t(`customer.screen.kind.${k}`) }))} />
        <Button type="submit" size="sm" variant="outline" disabled={busy || !name.trim()}>
          <Search className="size-3.5" /> {busy ? t("state.loading") : t("customer.screen.run")}
        </Button>
      </form>
      {err && <ErrorState compact error={err} onRetry={() => run({ preventDefault() {} })} />}
      {res && (
        <div className="flex flex-col gap-2" aria-live="polite">
          {res.status === "clear" && (
            <p className="flex flex-wrap items-center gap-2 text-sm">
              <StatusBadge size="sm" state="success" label={t("enum.sanctions.clear")} />
              <span className="text-ink-secondary">{t("customer.screen.clear", { lists, score: scorePct(res.top_score, locale) })}</span>
            </p>
          )}
          {res.status === "potential_match" && (
            <p className="flex items-center gap-2 text-sm">
              <StatusBadge size="sm" state="flagged" label={t("enum.sanctions.potential_match")} />
              <span className="text-ink-secondary">{t("customer.screen.hits", { n: res.hits.length })}</span>
            </p>
          )}
          {res.status === "error" && <ErrorState compact error={{ message: res.reason || t("state.error") }} />}
          {res.complete === false && res.status !== "error" && <p className="text-xs text-warning-fg">{t("customer.screen.incomplete")}</p>}
          <ScreeningHits hits={res.hits} />
        </div>
      )}
    </div>
  );
}
