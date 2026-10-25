// Ops customer page. Decision: approve / reject KYC (with the sanctions decision when a name matched),
// then the commercial and customs terms we work on with this customer.
import React, { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { KV, PageHeader, Section } from "@/components/app/layout";
import { StatusBadge } from "@/components/app/StatusBadge";
import CreditPanel from "@/features/commercial/CreditPanel";
import { Timeline } from "@/components/app/Timeline";
import { EmptyState, ErrorState, Freshness, LoadingRows, Loadable } from "@/components/app/states";
import { apiPatch, apiPost, useApi } from "@/lib/api";
import { date, dateShort, dateTime, money, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { KycChecklist, RunChecksAction, checksNeverRun } from "@/features/company/KycChecklist";
import { UboList } from "@/features/company/UboEditor";
import { PoaSummary } from "@/features/company/PoaSign";
import { ScreeningCheck, ScreeningLists } from "@/features/company/ScreeningHits";
import { CHECK_STATE, DemoTag, Field, SanctionsBadge, actorName, centsToInput, checkSummary, parseEuro } from "@/features/company/common";
import { EoriVerify } from "@/features/company/EoriVerify";

const RISK = ["ytunnus", "ytj", "vies", "sanctions", "ubo", "pep"];

// --------------------------------------------------------------------------------------------
// KYC decision
// --------------------------------------------------------------------------------------------
function DecisionPanel({ c, onDone }) {
  const { t, locale } = useT();
  const k = c.kyc || {};
  const open = ["in_review", "not_started", "pending"].includes(c.kyc_status) || c.sanctions_status === "potential_match";
  const [editing, setEditing] = useState(false);
  const [note, setNote] = useState("");
  const [sanctions, setSanctions] = useState(null);
  const [busy, setBusy] = useState(null);
  const [err, setErr] = useState(null);
  const notGreen = (c.kyc_checklist || []).filter((x) => RISK.includes(x.key) && !["pass", "not_applicable", "not_run"].includes(x.status));
  const neverRun = checksNeverRun(c.kyc_checklist);
  const match = c.sanctions_status === "potential_match";
  const noteRequired = k.green === false;

  if (!c.can?.decide) return <p className="text-xs text-ink-tertiary">{t("customer.decision.no_perm")}</p>;

  const decide = async (decision) => {
    setErr(null);
    if (decision !== "in_review" && noteRequired && !note.trim()) {
      setErr({ message: t("customer.decision.note_required") });
      return;
    }
    if (decision === "approved" && match && sanctions !== "clear") {
      setErr({ message: t("customer.decision.sanctions_required") });
      return;
    }
    setBusy(decision);
    try {
      const res = await apiPost(`/ops/customers/${c.id}/kyc-decision`, {
        decision, note: note.trim() || null, sanctions_decision: match ? sanctions : null,
      });
      toast.success(t(`customer.decision.done.${decision}`));
      setNote("");
      setSanctions(null);
      setEditing(false);
      onDone(res);
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(null);
    }
  };

  const decidedLine = k.decided_at && (
    <p className="text-sm text-ink-secondary">
      {t("customer.decision.decided", { decision: tEnum(t, "kyc", k.decision || c.kyc_status), name: actorName(k.decided_by) || "—", time: dateTime(k.decided_at, locale) })}
      {k.note && <span className="block text-xs text-ink-tertiary">“{k.note}”</span>}
    </p>
  );
  const sd = k.sanctions_decision;

  if (!open && !editing) {
    return (
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          {decidedLine}
          {sd && <p className="text-xs text-ink-tertiary">{t("customer.decision.sanctions_recorded", { decision: tEnum(t, "sanctions", sd.decision), name: actorName(sd.by) || "—", time: dateTime(sd.at, locale) })}</p>}
        </div>
        <Button size="sm" variant="outline" onClick={() => setEditing(true)}>{t("customer.decision.change")}</Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {decidedLine}
      {neverRun ? (
        <p className="text-sm text-warning-fg">{t("customer.decision.not_run")}</p>
      ) : notGreen.length ? (
        <div>
          <p className="mb-1 text-xs text-ink-tertiary">{t("customer.decision.why")}</p>
          <ul className="flex flex-col gap-1">
            {notGreen.map((x) => (
              <li key={x.key} className="flex flex-wrap items-baseline gap-x-2 text-sm">
                <StatusBadge size="sm" state={CHECK_STATE[x.status]} label={t(`company.cs.${x.status}`)} />
                <span className="font-medium">{t(`company.check.${x.key}`)}</span>
                <span className="text-ink-secondary">{checkSummary(x, locale)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-sm text-ink-secondary">{t("customer.decision.all_green")}</p>
      )}
      {match && (
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-xs text-ink-secondary">{t("customer.decision.sanctions")}</legend>
          <p className="text-xs text-ink-tertiary">{t("customer.decision.sanctions_required")}</p>
          <RadioGroup value={sanctions || ""} onValueChange={setSanctions} className="gap-1.5">
            {[["clear", "customer.decision.sanctions_clear"], ["blocked", "customer.decision.sanctions_block"]].map(([v, key]) => (
              <label key={v} className="flex items-center gap-2 text-sm"><RadioGroupItem value={v} /> {t(key)}</label>
            ))}
          </RadioGroup>
        </fieldset>
      )}
      <Field label={t("customer.decision.note")} htmlFor="kyc-note" hint={noteRequired ? t("customer.decision.note_required") : null}>
        <Textarea id="kyc-note" value={note} onChange={(e) => setNote(e.target.value)} className="min-h-[60px]" disabled={!!busy} />
      </Field>
      {err && <ErrorState compact error={err} />}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={() => decide("approved")} disabled={!!busy || neverRun || sanctions === "blocked"}>
          {busy === "approved" ? t("state.saving") : t("customer.decision.approve")}
        </Button>
        <Button size="sm" variant="destructive" onClick={() => decide("rejected")} disabled={!!busy}>
          {busy === "rejected" ? t("state.saving") : t("customer.decision.reject")}
        </Button>
        {c.kyc_status !== "in_review" && (
          <Button size="sm" variant="outline" onClick={() => decide("in_review")} disabled={!!busy}>{t("customer.decision.keep")}</Button>
        )}
        {editing && <Button size="sm" variant="ghost" onClick={() => { setEditing(false); setErr(null); }} disabled={!!busy}>{t("action.cancel")}</Button>}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------------------------
// Commercial & customs terms
// --------------------------------------------------------------------------------------------
function termsFrom(c) {
  return {
    credit: centsToInput(c.credit_limit_cents), terms: c.payment_terms_days ?? "", representation: c.representation_default || "",
    billing_email: c.billing_email || "", guarantee_ref: c.guarantee_ref || "", guarantee_amount: centsToInput(c.guarantee_amount_cents),
    deferred: c.deferred_payment_authorisation_no || "", cbam: c.cbam_declarant_id || "", vat_registered: !!c.vat_registered,
  };
}

function TermsForm({ c, onSaved }) {
  const { t } = useT();
  const init = useMemo(() => termsFrom(c), [c]);
  const [f, setF] = useState(init);
  const [base, setBase] = useState(init);
  const [note, setNote] = useState("");
  const [errors, setErrors] = useState({});
  const [err, setErr] = useState(null);
  const [saving, setSaving] = useState(false);
  const canEdit = !!c.can?.edit;
  const dirty = JSON.stringify(f) !== JSON.stringify(base);
  const toIndirect = f.representation === "indirect" && base.representation !== "indirect";
  const set = (k) => (e) => { const v = e?.target ? e.target.value : e; setF((p) => ({ ...p, [k]: v })); setErrors((p) => ({ ...p, [k]: undefined })); };

  const save = async (e) => {
    e.preventDefault();
    const errs = {};
    const patch = {};
    if (f.credit !== base.credit) {
      const v = parseEuro(f.credit);
      if (v === null || Number.isNaN(v)) errs.credit = t("customer.terms.bad_amount");
      else patch.credit_limit_cents = v;
    }
    if (String(f.terms) !== String(base.terms)) {
      const n = Number(f.terms);
      if (!Number.isInteger(n) || n < 0 || n > 120) errs.terms = "0–120";
      else patch.payment_terms_days = n;
    }
    if (f.guarantee_amount !== base.guarantee_amount) {
      const v = parseEuro(f.guarantee_amount);
      if (Number.isNaN(v)) errs.guarantee_amount = t("customer.terms.bad_amount");
      else patch.guarantee_amount_cents = v;
    }
    if (f.representation !== base.representation && f.representation) patch.representation_default = f.representation;
    if (f.billing_email !== base.billing_email) patch.billing_email = f.billing_email.trim() || null;
    if (f.guarantee_ref !== base.guarantee_ref) patch.guarantee_ref = f.guarantee_ref.trim() || null;
    if (f.deferred !== base.deferred) patch.deferred_payment_authorisation_no = f.deferred.trim() || null;
    if (f.cbam !== base.cbam) patch.cbam_declarant_id = f.cbam.trim() || null;
    if (f.vat_registered !== base.vat_registered) patch.vat_registered = f.vat_registered;
    if (toIndirect && !note.trim()) errs.note = t("customer.terms.indirect_note");
    setErrors(errs);
    if (Object.keys(errs).length) return;
    setSaving(true);
    setErr(null);
    try {
      const res = await apiPatch(`/ops/customers/${c.id}`, { ...patch, note: note.trim() || null });
      const next = termsFrom(res);
      setF(next);
      setBase(next);
      setNote("");
      toast.success(t("customer.terms.saved"));
      onSaved(res);
    } catch (ex) {
      setErr(ex);
    } finally {
      setSaving(false);
    }
  };
  const dis = !canEdit || saving;

  return (
    <form onSubmit={save} className="flex flex-col gap-3" noValidate>
      {!canEdit && <p className="text-xs text-ink-tertiary">{t("customer.terms.read_only")}</p>}
      <div className="grid grid-cols-2 gap-3">
        <Field label={t("customer.terms.credit")} htmlFor="tm-credit" error={errors.credit}>
          <Input id="tm-credit" value={f.credit} onChange={set("credit")} disabled={dis} inputMode="decimal" className="tabular text-right" />
        </Field>
        <Field label={t("customer.terms.payment_terms")} htmlFor="tm-terms" error={errors.terms}>
          <Input id="tm-terms" value={f.terms} onChange={set("terms")} disabled={dis} inputMode="numeric" className="tabular text-right" />
        </Field>
      </div>
      <Field label={t("customer.terms.representation")} htmlFor="tm-rep" hint={f.representation === "indirect" ? t("customer.terms.indirect_note") : null}>
        <Select value={f.representation || undefined} onValueChange={set("representation")} disabled={dis}>
          <SelectTrigger id="tm-rep"><SelectValue placeholder="—" /></SelectTrigger>
          <SelectContent>
            {["direct", "indirect"].map((r) => <SelectItem key={r} value={r}>{t(`company.poa.type.${r}`)}</SelectItem>)}
          </SelectContent>
        </Select>
      </Field>
      <Field label={t("customer.terms.billing_email")} htmlFor="tm-bill">
        <Input id="tm-bill" type="email" value={f.billing_email} onChange={set("billing_email")} disabled={dis} />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label={t("customer.terms.guarantee_ref")} htmlFor="tm-gref">
          <Input id="tm-gref" value={f.guarantee_ref} onChange={set("guarantee_ref")} disabled={dis} className="font-mono" />
        </Field>
        <Field label={t("customer.terms.guarantee_amount")} htmlFor="tm-gamt" error={errors.guarantee_amount}>
          <Input id="tm-gamt" value={f.guarantee_amount} onChange={set("guarantee_amount")} disabled={dis} inputMode="decimal" className="tabular text-right" />
        </Field>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Field label={t("customer.terms.deferred")} htmlFor="tm-def">
          <Input id="tm-def" value={f.deferred} onChange={set("deferred")} disabled={dis} className="font-mono" />
        </Field>
        <Field label={t("customer.terms.cbam")} htmlFor="tm-cbam">
          <Input id="tm-cbam" value={f.cbam} onChange={set("cbam")} disabled={dis} className="font-mono" />
        </Field>
      </div>
      <label className="flex items-center justify-between gap-3 text-sm">
        {t("customer.terms.vat_registered")}
        <Switch aria-label={t("customer.terms.vat_registered")} checked={f.vat_registered} onCheckedChange={(v) => setF((p) => ({ ...p, vat_registered: !!v }))} disabled={dis} />
      </label>
      {canEdit && dirty && (
        <Field label={t("customer.terms.note")} htmlFor="tm-note" error={errors.note}>
          <Textarea id="tm-note" value={note} onChange={(e) => { setNote(e.target.value); setErrors((p) => ({ ...p, note: undefined })); }} className="min-h-[56px]" disabled={saving} />
        </Field>
      )}
      {err && <ErrorState compact error={err} />}
      {canEdit && (
        <div className="flex items-center gap-2">
          <Button type="submit" size="sm" disabled={!dirty || saving}>{saving ? t("state.saving") : t("action.save")}</Button>
          {dirty && !saving && <Button type="button" size="sm" variant="ghost" onClick={() => { setF(base); setErrors({}); setNote(""); }}>{t("action.cancel")}</Button>}
        </div>
      )}
    </form>
  );
}

// --------------------------------------------------------------------------------------------
// Activity
// --------------------------------------------------------------------------------------------
const FIELD_LABEL = {
  business_id: "company.profile.business_id", name: "company.profile.name", address: "customer.company.address",
  vat_id: "company.profile.vat", eori: "company.profile.eori", billing_email: "company.profile.billing_email",
  ubos: "company.ubo.title", ubo_none_declared: "company.ubo.title", signatory: "company.signatory.title",
  cbam_declarant_id: "company.profile.cbam_id", cbam_under_threshold: "company.profile.cbam_under",
  source_of_business: "review.field.source_of_business", credit_limit_cents: "customer.terms.credit_limit", payment_terms_days: "customer.terms.payment_terms", vat_registered: "customer.terms.vat_registered",
};

function auditDetail(e, t, locale) {
  const d = e.data || {};
  if (d.changes) {
    return Object.entries(d.changes).map(([k, v]) => {
      const fmt = (x) => (x === null || x === undefined || x === "" ? "—" : k.endsWith("_cents") ? money(x, locale) : typeof x === "object" ? JSON.stringify(x) : String(x));
      return `${FIELD_LABEL[k] ? t(FIELD_LABEL[k]) : k.replace(/_cents$/, "").replace(/_/g, " ")}: ${fmt(v.from)} → ${fmt(v.to)}`;
    }).join("; ") + (d.note ? ` · “${d.note}”` : "");
  }
  if (d.risk) {
    const bad = Object.entries(d.risk).filter(([, v]) => !["pass", "not_applicable"].includes(v));
    const head = d.status ? tEnum(t, "kyc", d.status) : null;
    const tail = bad.length ? bad.map(([k, v]) => `${t(`company.check.${k}`)}: ${t(`company.cs.${v}`)}`).join(" · ") : t("customer.decision.all_green");
    return [head, tail].filter(Boolean).join(" · ");
  }
  if (d.note) return `“${d.note}”`;
  if (d.valid_to) return `${(d.type && t(`company.poa.type.${d.type}`)) || ""} → ${date(d.valid_to, locale)}`;
  if (d.fields) return Array.isArray(d.fields) ? [...new Set(d.fields.map((f) => (FIELD_LABEL[f] ? t(FIELD_LABEL[f]) : f.replace(/_/g, " "))))].join(", ") : null;
  return null;
}

function AuditLog({ audit }) {
  const { t, locale } = useT();
  const [all, setAll] = useState(false);
  if (!audit?.length) return <p className="text-sm text-ink-tertiary">{t("customer.audit.empty")}</p>;
  const shown = all ? audit : audit.slice(0, 8);
  const events = shown.map((e) => {
    const key = `customer.audit.action.${e.action}`;
    const title = t(key) === key ? e.action.replace(/_/g, " ") : t(key);
    return {
      id: e.id, at: e.at, title, source: actorName(e.actor),
      detail: auditDetail(e, t, locale),
      tone: /rejected|revoked|blocked/.test(e.action) ? "danger" : undefined,
    };
  });
  return (
    <div className="flex flex-col gap-2">
      <Timeline events={events} />
      {audit.length > 8 && !all && (
        <Button size="xs" variant="ghost" className="self-start" onClick={() => setAll(true)}>{t("customer.audit.show_all", { n: audit.length })}</Button>
      )}
    </div>
  );
}

function Users({ users }) {
  const { t } = useT();
  if (!users?.length) return <p className="text-sm text-ink-tertiary">{t("customer.users.empty")}</p>;
  return (
    <ul className="divide-y divide-[color:var(--border-subtle)]">
      {users.map((u) => (
        <li key={u.id} className="flex flex-wrap items-baseline justify-between gap-x-3 py-2">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{u.name}</p>
            <p className="truncate text-xs text-ink-tertiary">{u.email}</p>
          </div>
          <span className="flex items-center gap-2 text-xs text-ink-secondary">
            {u.disabled ? t("customer.users.disabled") : !u.email_verified ? <span className="text-warning-fg">{t("customer.users.unverified")}</span> : null}
            {tEnum(t, "role", u.role)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Shipments({ items, total }) {
  const { t, locale } = useT();
  if (!items?.length) return <p className="text-sm text-ink-tertiary">{t("customer.shipments.empty")}</p>;
  return (
    <ul className="divide-y divide-[color:var(--border-subtle)]">
      {items.map((s) => (
        <li key={s.id}>
          <Link to={`/ops/shipments/${s.id}`} className="flex items-center justify-between gap-3 py-2 hover:bg-hover">
            <span className="min-w-0">
              <span className="block font-mono text-[13px]">{s.reference}</span>
              <span className="block text-xs text-ink-tertiary">{[tEnum(t, "mode", s.mode), tEnum(t, "direction", s.direction), s.eta ? `${t("field.eta")} ${dateShort(s.eta, locale)}` : null].filter(Boolean).join(" · ")}</span>
            </span>
            <StatusBadge group="status" value={s.status} size="sm" />
          </Link>
        </li>
      ))}
      {total > items.length && (
        <li className="pt-2"><Link to="/ops/shipments" className="text-xs text-ink-secondary hover:underline">{t("customers.shipments_n", { n: total })}</Link></li>
      )}
    </ul>
  );
}

function Declarations({ items }) {
  const { t, locale } = useT();
  if (!items?.length) return <p className="text-sm text-ink-tertiary">{t("customer.decl.empty")}</p>;
  return (
    <ul className="divide-y divide-[color:var(--border-subtle)]">
      {items.map((d) => (
        <li key={d.id}>
          <Link to={`/ops/customs/${d.id}`} className="flex items-center justify-between gap-3 py-2 hover:bg-hover">
            <span className="min-w-0">
              <span className="block font-mono text-[13px]">{d.mrn || d.lrn}</span>
              <span className="block text-xs text-ink-tertiary">{[d.shipment_ref, relTime(d.updated_at, locale)].filter(Boolean).join(" · ")}</span>
            </span>
            <span className="flex items-center gap-3">
              <span className="hidden text-xs tabular text-ink-secondary sm:inline">{money(d.totals?.customs_value_cents, locale)}</span>
              <StatusBadge group="customs" value={d.status} size="sm" />
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

// --------------------------------------------------------------------------------------------
// Page
// --------------------------------------------------------------------------------------------
function Body({ q }) {
  const { t, locale } = useT();
  const c = q.data;
  const [running, setRunning] = useState(false);
  const [runErr, setRunErr] = useState(null);
  const y = c.ytj || {};
  const a = c.address || {};
  const checkedAt = checksNeverRun(c.kyc_checklist) ? null : c.kyc_checked_at || c.kyc?.run_at;

  const rerun = async () => {
    setRunning(true);
    setRunErr(null);
    try {
      q.setData(await apiPost(`/ops/customers/${c.id}/kyc/run`, {}));
      toast.success(t("company.checks.done"));
    } catch (e) {
      setRunErr(e);
      q.reload();
    } finally {
      setRunning(false);
    }
  };

  return (
    <>
      <PageHeader
        crumbs={[{ label: t("customers.title"), to: "/ops/customers" }, { label: c.name }]}
        title={c.name}
        meta={<>
          <span className="font-mono text-[13px]">{c.business_id || "—"}</span>
          <StatusBadge group="kyc" value={c.kyc_status} size="sm" />
          <SanctionsBadge value={c.sanctions_status} />
          {c.status === "blocked" && <StatusBadge size="sm" state="failed" label={t("customers.blocked")} />}
          {c.demo && <DemoTag />}
        </>}
        actions={<Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />}
      />
      <div className="grid gap-x-8 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="min-w-0">
          <Section title={t("customer.decision.title")}>
            <DecisionPanel c={c} onDone={(res) => q.setData(res)} />
          </Section>
          <Section title={t("company.checks.title")}
            actions={<RunChecksAction checkedAt={checkedAt} running={running} onRun={rerun} canRun={c.can?.run_kyc}
              disabledReason={!c.business_id ? t("company.checks.need_bid") : null} label={t("customer.rerun")} />}>
            {runErr && <ErrorState className="mb-2" compact error={runErr} onRetry={rerun} />}
            {c.kyc_stale && <p className="mb-2 text-xs text-warning-fg">{t("company.stale")}</p>}
            <KycChecklist checklist={c.kyc_checklist} audience="ops" running={running}
              renderExtra={(row) => (row.key === "eori" ? <EoriVerify company={c} row={row} onDone={(res) => q.setData(res)} /> : null)} />
          </Section>
          <Section title={t("customer.company.title")}>
            <KV cols={2} items={[
              { label: t("customer.company.ytj_name"), value: y.name, hint: y.name && c.name && y.name !== c.name ? `≠ ${c.name}` : null },
              { label: t("customer.company.form"), value: y.company_form },
              { label: t("customer.company.registered"), value: y.registration_date ? date(y.registration_date, locale) : null },
              { label: t("customer.company.trade_register"), value: y.trade_register_status ? tEnum(t, "trade_register", y.trade_register_status) : null },
              { label: t("customer.company.business_line"), value: y.main_business_line },
              { label: t("customer.company.address"), value: [a.street, [a.postal_code, a.city].filter(Boolean).join(" "), a.country].filter(Boolean).join(", ") || null },
              { label: t("company.profile.vat"), value: c.vat_id ? <span className="font-mono text-[13px]">{c.vat_id}</span> : null, hint: c.vat_registered === true ? t("company.profile.vat_registered") : c.vat_registered === false ? t("company.profile.not_vat_registered") : null },
              { label: t("company.profile.eori"), value: c.eori, mono: true },
              { label: t("company.profile.billing_email"), value: c.billing_email },
              { label: t("customer.company.created"), value: date(c.created_at, locale) },
            ]} />
          </Section>
          <Section title={t("company.ubo.title")}>
            <UboList ubos={c.ubos} noneDeclared={c.ubo_none_declared} />
            {c.signatory?.name && (
              <p className="mt-2 text-xs text-ink-tertiary">
                {t("company.signatory.title")}: <span className="text-foreground">{[c.signatory.name, c.signatory.title].filter(Boolean).join(", ")}</span>
                {c.signatory.email ? ` · ${c.signatory.email}` : ""}
              </p>
            )}
          </Section>
          <Section title={t("company.poa.title")}>
            <PoaSummary company={c} audience="ops" />
          </Section>
          <Section title={t("customer.lists.title")}>
            <ScreeningLists lists={c.screening_lists} />
            <div className="mt-4">
              <h3 className="mb-2 text-xs font-medium text-ink-secondary">{t("customer.screen.title")}</h3>
              <ScreeningCheck />
            </div>
          </Section>
        </div>
        <div className="min-w-0">
          <Section title={t("customer.terms.title")}>
            <TermsForm key={c.updated_at || c.id} c={c} onSaved={(res) => q.setData(res)} />
          </Section>
          <CreditPanel companyId={c.id} />
          <Section title={t("customer.decl.title")}><Declarations items={c.declarations} /></Section>
          <Section title={t("customer.shipments.title")}><Shipments items={c.recent_shipments} total={c.shipments_count} /></Section>
          <Section title={t("customer.users.title")}><Users users={c.users} /></Section>
          <Section title={t("customer.audit.title")}><AuditLog audit={c.audit} /></Section>
        </div>
      </div>
    </>
  );
}

export default function CustomerDetail() {
  const { id } = useParams();
  const { t } = useT();
  const q = useApi(`/ops/customers/${id}`);
  if (q.error?.status === 404 && !q.data) {
    return (
      <div className="max-w-[1280px]">
        <PageHeader crumbs={[{ label: t("customers.title"), to: "/ops/customers" }]} title={t("customer.not_found")} />
        <EmptyState title={t("customer.not_found")} action={<Button asChild size="sm" variant="outline"><Link to="/ops/customers">{t("action.back")}</Link></Button>} />
      </div>
    );
  }
  return (
    <div className={cn("max-w-[1280px]")}>
      <Loadable q={q} skeleton={<LoadingRows rows={10} />}>{() => <Body q={q} />}</Loadable>
    </div>
  );
}
