// Dangerous goods: UN-number lookup (class, packing groups, IATA aircraft limits, ADR tunnel code, sources) and the
// same cargo check that quote and booking run. Review / forbidden is for a trained person (IMDG/ADR 1.3, IATA DGR 1.5).
import React, { useState } from "react";
import { Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { KV, Section } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { StatusBadge } from "@/components/app/StatusBadge";
import { apiGet, apiPost } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { ApiFail, Hairline, fieldCls, label } from "./common";

const MODES = ["sea", "air", "road", "rail"];
const PGS = ["", "I", "II", "III"];
const RESULT_STATE = { ok: "success", review: "in-review", forbidden: "failed" };
const SEVERITY_STATE = { info: "expired", warning: "pending", review: "in-review", error: "failed", forbidden: "failed", block: "failed" };
const normUn = (v) => String(v || "").toUpperCase().replace(/^UN\s*/, "").trim();

function Limit({ v }) {
  const { t } = useT();
  if (v?.forbidden) return <StatusBadge size="sm" state="failed" label={t("compliance.dg.forbidden")} />;
  return v?.limit ? <span className="tabular">{v.limit}</span> : <span className="text-ink-tertiary">—</span>;
}

function UnResult({ r }) {
  const { t } = useT();
  const variants = r.variants || [];
  return (
    <div className="flex flex-col gap-3" aria-live="polite">
      <div>
        <p className="text-sm font-medium"><span className="mr-2 font-mono">{String(r.un || "").startsWith("UN") ? r.un : `UN${r.un}`}</span>{(r.names || [])[0] || "—"}</p>
        {(r.names || []).length > 1 && <p className="text-xs text-ink-tertiary">{r.names.slice(1).join(" · ")}</p>}
      </div>
      <KV cols={3} items={[
        { label: t("compliance.dg.class"), value: r.class, mono: true },
        { label: t("compliance.dg.subsidiary"), value: (r.subsidiary || []).join(", ") || null, mono: true },
        { label: t("compliance.dg.pg"), value: (r.packing_groups || []).join(", ") || null },
        { label: t("compliance.dg.tunnel"), value: r.adr?.tunnel_code, mono: true },
        { label: t("compliance.dg.transport_category"), value: r.adr?.transport_category ?? null, mono: true },
      ]} />
      {variants.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[520px] border-collapse text-sm">
            <thead>
              <tr className="border-b border-[color:var(--border-default)] text-left text-xs text-ink-tertiary">
                <th scope="col" className="py-1.5 pr-3 font-medium">{t("compliance.dg.pg")}</th>
                <th scope="col" className="py-1.5 pr-3 font-medium">{t("compliance.dg.names")}</th>
                <th scope="col" className="py-1.5 pr-3 font-medium">{t("compliance.dg.air")} · {t("compliance.dg.passenger")}</th>
                <th scope="col" className="py-1.5 pr-3 font-medium">{t("compliance.dg.air")} · {t("compliance.dg.cargo")}</th>
              </tr>
            </thead>
            <tbody>
              {variants.map((v, i) => (
                <tr key={i} className="border-b border-[color:var(--border-subtle)] align-top">
                  <td className="py-1.5 pr-3">{v.pg || "—"}</td>
                  <td className="py-1.5 pr-3">{v.name || "—"}</td>
                  <td className="py-1.5 pr-3"><Limit v={{ limit: v.air?.passenger_limit, forbidden: v.air?.passenger_forbidden }} /></td>
                  <td className="py-1.5 pr-3"><Limit v={{ limit: v.air?.cargo_limit, forbidden: v.air?.cargo_forbidden }} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Sources sources={r.sources} />
    </div>
  );
}

/** Sources as links: strings or {label|name|title, url, date|version}. */
export function Sources({ sources }) {
  const { t } = useT();
  if (!sources?.length) return null;
  return (
    <p className="text-xs text-ink-tertiary">
      {t("compliance.dg.sources")}:{" "}
      {sources.map((s, i) => {
        const o = typeof s === "string" ? { label: s, url: /^https?:/.test(s) ? s : null } : s;
        const text = [o.label || o.name || o.title || o.url, o.version || o.date].filter(Boolean).join(" ");
        return (
          <React.Fragment key={i}>
            {i > 0 && " · "}
            {o.url ? <a href={o.url} target="_blank" rel="noreferrer" className="underline underline-offset-2 hover:text-foreground">{text}</a> : text}
          </React.Fragment>
        );
      })}
    </p>
  );
}

function Lookup() {
  const { t } = useT();
  const [un, setUn] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const n = normUn(un);
  const valid = /^\d{4}$/.test(n);
  const run = async (e) => {
    e?.preventDefault();
    if (!valid) return;
    setBusy(true);
    setErr(null);
    setRes(null);
    try { setRes(await apiGet(`/compliance/dg/un/${n}`)); } catch (ex) { setErr(ex); } finally { setBusy(false); }
  };
  return (
    <Section title={t("compliance.dg.lookup")}>
      <form onSubmit={run} className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start">
        <div className="sm:w-48">
          <Input value={un} onChange={(e) => setUn(e.target.value)} placeholder={t("compliance.dg.un_placeholder")} aria-label={t("compliance.dg.un")}
            inputMode="numeric" className="font-mono" aria-invalid={un !== "" && !valid} />
          {un !== "" && !valid && <p className="mt-1 text-xs text-danger-fg">{t("compliance.dg.invalid_un")}</p>}
        </div>
        <Button type="submit" size="sm" variant="outline" disabled={busy || !valid}><Search className="size-3.5" /> {busy ? t("state.loading") : t("compliance.dg.find")}</Button>
      </form>
      {err && <ApiFail compact error={err} onRetry={run} />}
      {res && <UnResult r={res} />}
    </Section>
  );
}

function Check() {
  const { t, locale } = useT();
  const [f, setF] = useState({ un_number: "", class: "", packing_group: "", proper_shipping_name: "" });
  const [mode, setMode] = useState("sea");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const run = async (e) => {
    e?.preventDefault();
    setBusy(true);
    setErr(null);
    setRes(null);
    const dg = { ...f, un_number: normUn(f.un_number) };
    Object.keys(dg).forEach((k) => { if (!dg[k]) delete dg[k]; });
    try { setRes(await apiPost("/compliance/dg/check", { cargo: { dangerous_goods: true, dg }, mode })); } catch (ex) { setErr(ex); } finally { setBusy(false); }
  };
  return (
    <Section title={t("compliance.dg.check")}>
      <p className="mb-3 text-xs text-ink-tertiary">{t("compliance.dg.check_hint")}</p>
      <form onSubmit={run} className="mb-3 flex flex-col gap-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="text-sm">{t("compliance.dg.un")}<Input className="mt-1 font-mono" value={f.un_number} onChange={set("un_number")} placeholder={t("compliance.dg.un_placeholder")} /></label>
          <label className="text-sm">{t("compliance.dg.class")}<Input className="mt-1 font-mono" value={f.class} onChange={set("class")} placeholder="3" /></label>
          <label className="text-sm">{t("compliance.dg.pg")}
            <select className={fieldCls} value={f.packing_group} onChange={set("packing_group")}>
              {PGS.map((p) => <option key={p} value={p}>{p || t("compliance.dg.none")}</option>)}
            </select>
          </label>
          <label className="text-sm">{t("compliance.dg.psn")}<Input className="mt-1" value={f.proper_shipping_name} onChange={set("proper_shipping_name")} /></label>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Segmented value={mode} onChange={setMode} label={t("compliance.dg.mode")} options={MODES.map((m) => ({ value: m, label: t(`compliance.mode.${m}`) }))} />
          <Button type="submit" size="sm" disabled={busy || !f.un_number.trim()}>{busy ? t("state.loading") : t("compliance.dg.run_check")}</Button>
        </div>
      </form>
      {err && <ApiFail compact error={err} onRetry={run} />}
      {res && (
        <div className="flex flex-col gap-2" aria-live="polite">
          <p className="flex flex-wrap items-center gap-2 text-sm">
            <StatusBadge size="sm" state={RESULT_STATE[res.status] || "expired"} label={label(t, "compliance.dg.result", res.status)} />
            {res.tunnel_code && <span className="text-ink-secondary">{t("compliance.dg.tunnel")} <span className="font-mono">{res.tunnel_code}</span></span>}
          </p>
          {(res.findings || []).length ? (
            <Hairline>
              {res.findings.map((x, i) => (
                <li key={`${x.code}-${i}`} className="flex flex-wrap items-start gap-x-3 gap-y-1 px-3 py-2">
                  <StatusBadge size="sm" state={SEVERITY_STATE[x.severity] || "expired"} label={label(t, "compliance.sev", x.severity)} />
                  <span className="min-w-0 flex-1 text-sm">{(locale === "fi" ? x.fi : x.en) || x.en || x.fi}</span>
                  <span className="font-mono text-[11px] text-ink-tertiary">{x.code}</span>
                </li>
              ))}
            </Hairline>
          ) : <p className="text-sm text-ink-tertiary">{t("compliance.dg.no_findings")}</p>}
        </div>
      )}
    </Section>
  );
}

export default function DangerousGoods() {
  return (
    <div className="grid gap-x-8 xl:grid-cols-2">
      <div className="min-w-0"><Lookup /></div>
      <div className="min-w-0"><Check /></div>
    </div>
  );
}
