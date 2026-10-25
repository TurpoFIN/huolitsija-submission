// PEP check (politically exposed persons and close associates) for beneficial owners and representatives.
// A match is a lead with its source record (e.g. Wikidata QID + positions with dates), never an automatic decision.
import React, { useState } from "react";
import { Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import { apiPost } from "@/lib/api";
import { date } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { ApiFail, Hairline, label } from "./common";
import { Sources } from "./DangerousGoods";

const PEP_STATE = { pep: "flagged", rca: "pending", clear: "success", error: "failed" };

function Period({ p }) {
  const { t, locale } = useT();
  const fmt = (v) => (v ? (/^\d{4}$/.test(String(v)) ? v : date(v, locale)) : null);
  const s = fmt(p.start);
  const e = p.end ? fmt(p.end) : t("compliance.pep.current");
  return <span className="tabular text-ink-tertiary">{s ? `${s} – ${e}` : p.end ? `– ${e}` : ""}</span>;
}

function Match({ m }) {
  const { t } = useT();
  return (
    <li className="flex flex-col gap-1 px-3 py-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <p className="text-sm font-medium">{m.url ? <a href={m.url} target="_blank" rel="noreferrer" className="hover:underline">{m.name}</a> : m.name}</p>
        {m.qid && <span className="font-mono text-[11px] text-ink-tertiary">{m.qid}</span>}
      </div>
      {m.relation && <p className="text-xs text-ink-secondary">{t("compliance.pep.relation")}: {m.relation}</p>}
      {(m.positions || []).length > 0 && (
        <ul className="flex flex-col gap-0.5 text-xs">
          {m.positions.map((p, i) => (
            <li key={i} className="flex flex-wrap gap-x-2">
              <span>{p.label}</span>
              <Period p={p} />
              {p.country && <span className="text-ink-tertiary">· {p.country}</span>}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export default function Pep() {
  const { t } = useT();
  const [name, setName] = useState("");
  const [country, setCountry] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const cc = country.trim().toUpperCase();
  const ccOk = !cc || /^[A-Z]{2}$/.test(cc);
  const run = async (e) => {
    e?.preventDefault();
    if (!name.trim() || !ccOk) return;
    setBusy(true);
    setErr(null);
    setRes(null);
    try { setRes(await apiPost("/compliance/pep-check", { name: name.trim(), ...(cc ? { country: cc } : {}) })); } catch (ex) { setErr(ex); } finally { setBusy(false); }
  };
  return (
    <Section title={t("compliance.pep.title")}>
      <p className="mb-3 max-w-prose text-xs text-ink-tertiary">{t("compliance.pep.hint")}</p>
      <form onSubmit={run} className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start">
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("compliance.pep.name")} aria-label={t("compliance.pep.name")} className="sm:flex-1" />
        <Input value={country} onChange={(e) => setCountry(e.target.value)} placeholder={t("compliance.pep.country")} aria-label={t("compliance.pep.country")}
          maxLength={2} className="font-mono sm:w-56" aria-invalid={!ccOk} />
        <Button type="submit" size="sm" variant="outline" disabled={busy || !name.trim() || !ccOk}><Search className="size-3.5" /> {busy ? t("state.loading") : t("compliance.pep.run")}</Button>
      </form>
      {err && <ApiFail compact error={err} onRetry={run} />}
      {res && (
        <div className="flex flex-col gap-2" aria-live="polite">
          <p className="flex flex-wrap items-center gap-2 text-sm">
            <StatusBadge size="sm" state={PEP_STATE[res.status] || "expired"} label={label(t, "compliance.pep.status", res.status)} />
            {res.status === "clear" && <span className="text-ink-secondary">{t("compliance.pep.clear_body")}</span>}
          </p>
          {res.status === "error" && res.error && <ApiFail compact error={{ message: res.error }} />}
          {(res.matches || []).length > 0 && <Hairline>{res.matches.map((m, i) => <Match key={m.qid || i} m={m} />)}</Hairline>}
          <Sources sources={res.sources} />
        </div>
      )}
    </Section>
  );
}
