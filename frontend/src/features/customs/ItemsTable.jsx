// Goods items. Ops: classification workbench (AI candidates, HS search, CN + TARIC confirmation, duty/VAT overrides).
// Portal: goods data the customer owns (description, origin, weights, packages), editable while draft/needs_info.
// Wide table scrolls inside its container; below 768px each item becomes a card.
import { useConfirmChange } from "@/components/app/ConfirmChange";
import { useAuth } from "@/lib/auth";
import React, { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, ExternalLink, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { AgentMark } from "@/components/app/StatusBadge";
import { dateTime, money, num } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { displayText } from "@/lib/localizedData";
import { cn } from "@/lib/utils";
import HsPicker from "./HsPicker";
import { packageOptions } from "./packageKinds";
import { FlagChips, fmtCode, invoiceMoney, parseNum, pctFmt, SaveState, useIsNarrow } from "./common";

const LOW = 0.7;
const conf = (it) => it.hs_suggestion?.confidence;
const isLow = (it) => typeof conf(it) === "number" && conf(it) < LOW && !(it.edited || []).includes("hs_code");
const pctText = (v) => `${Math.round(v * 100)} %`;

function codeSource(it) {
  if ((it.edited || []).includes("hs_code") || (it.edited || []).includes("cn_code")) return "broker";
  const sug = it.hs_suggestion?.source;
  return sug && sug !== "ai" ? sug : it.hs_source || sug || "broker";
}

function CodeSource({ it }) {
  const { user } = useAuth();
  const { t } = useT();
  if (!it.hs_code) return <span className="text-xs text-ink-tertiary">{t("customs.item.no_code")}</span>;
  const src = codeSource(it);
  if (src === "ai") {
    return (
      <span className="inline-flex items-center gap-1">
        <AgentMark label={typeof conf(it) === "number" ? t("customs.item.confidence", { pct: pctText(conf(it)) }) : undefined} />
        {isLow(it) && <span className="text-[11px] font-medium text-warning-fg">{t("customs.item.low_conf")}</span>}
      </span>
    );
  }
  if (src === "seed" && user?.org_type !== "internal") return null;
  const k = `customs.code_source.${src}`;
  const label = t(k);
  return <span className="text-[11px] text-ink-tertiary">{label === k ? src : label}</span>;
}

// CN 8-digit subdivisions offered for an HS6 code: [{code, description}] or ["94036010", …].
const cnOptions = (it) => (it.hs_suggestion?.cn_options || []).map((o) => (typeof o === "string" ? { code: o } : o)).filter((o) => o?.code);

function Candidates({ it, canEdit, onPatch }) {
  const { t } = useT();
  const cands = it.hs_candidates || [];
  const opts = cnOptions(it);
  const use = (body) => onPatch({ items: [{ seq: it.seq, ...body }] }, `item-${it.seq}`);
  return (
    <div className="flex flex-col gap-2">
      {opts.length > 0 && (
        <div>
          <p className="px-1 pb-1 text-xs font-medium text-ink-tertiary">{t("customs.item.cn_options", { code: fmtCode(it.hs_code) })}</p>
          <ul className="flex flex-col">
            {opts.map((o) => (
              <li key={o.code} className="flex items-start gap-2 rounded-lg px-1 py-1.5 hover:bg-hover">
                <span className="w-20 shrink-0 font-mono text-[13px]">{fmtCode(o.code)}</span>
                <span className="min-w-0 flex-1 text-sm leading-5">{o.description || "—"}</span>
                {it.cn_code === o.code ? (
                  <span className="w-12 shrink-0 text-right text-xs text-ink-tertiary">{t("customs.item.current")}</span>
                ) : (
                  <Button size="xs" variant="outline" className="w-12 shrink-0" disabled={!canEdit} onClick={() => use({ cn_code: o.code })}>
                    {t("customs.item.use")}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {cands.length > 0 && (
        <div>
          <p className="px-1 pb-1 text-xs font-medium text-ink-tertiary">{t("customs.item.candidates")}</p>
          <ul className="flex flex-col">
            {cands.map((c) => {
              const current = it.hs_code === c.code;
              return (
                <li key={c.code} className="flex items-start gap-2 rounded-lg px-1 py-1.5 hover:bg-hover">
                  <span className="w-20 shrink-0 font-mono text-[13px]">{fmtCode(c.code)}</span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm leading-5">{c.description}</span>
                    {c.rationale && <span className="block text-xs text-ink-tertiary">{c.rationale}</span>}
                  </span>
                  <span className="w-10 shrink-0 text-right text-xs tabular text-ink-secondary">
                    {typeof c.confidence === "number" ? pctText(c.confidence) : "—"}
                  </span>
                  {current ? (
                    <span className="w-12 shrink-0 text-right text-xs text-ink-tertiary">{t("customs.item.current")}</span>
                  ) : (
                    <Button size="xs" variant="outline" className="w-12 shrink-0" disabled={!canEdit} onClick={() => use({ hs_code: c.code })}>
                      {t("customs.item.use")}
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

/** CN/HS code; in ops it opens the CN subdivisions and AI candidates to pick from. */
function CodeCell({ it, mode, canEdit, onPatch }) {
  const { t } = useT();
  const code = it.cn_code || it.hs_code;
  const needCn = !!it.hs_code && !it.cn_code;
  const text = (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <span className="font-mono text-[13px] tabular">{fmtCode(code)}</span>
      {needCn && <span className="inline-flex h-5 items-center rounded-full px-1.5 text-[11px] font-medium status-pending">{t("customs.item.choose_cn")}</span>}
    </span>
  );
  const hasChoices = (it.hs_candidates || []).length > 0 || cnOptions(it).length > 0;
  if (mode !== "ops" || !hasChoices) return <div className="flex flex-col gap-0.5">{text}<CodeSource it={it} /></div>;
  return (
    <div className="flex flex-col items-start gap-0.5">
      <Popover>
        <PopoverTrigger asChild>
          <button type="button" className="inline-flex items-center gap-1 rounded-md px-1 -mx-1 hover:bg-hover" onClick={(e) => e.stopPropagation()}
            aria-label={needCn ? t("customs.item.choose_cn") : t("customs.item.candidates")}>
            {text}<ChevronDown className="size-3 text-ink-tertiary" />
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-[min(30rem,calc(100vw-24px))] max-h-[70vh] overflow-y-auto p-2 surface-overlay rounded-xl">
          <Candidates it={it} canEdit={canEdit} onPatch={onPatch} />
        </PopoverContent>
      </Popover>
      <CodeSource it={it} />
    </div>
  );
}

function TaricCell({ it, canEdit, onPatch }) {
  const { t } = useT();
  const confirmation = useConfirmChange();
  return (
    <div className="flex flex-col items-start gap-1">
      {it.taric_url ? (
        <a href={it.taric_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}
          className="inline-flex items-center gap-1 text-xs text-ink-accent hover:underline whitespace-nowrap">
          {t("customs.item.open_taric")} <ExternalLink className="size-3" />
        </a>
      ) : <span className="text-xs text-ink-tertiary">—</span>}
      <label className="inline-flex items-center gap-1.5 text-xs whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
        <Checkbox aria-label={`${t("customs.item.confirm_cn")}: ${fmtCode(it.cn_code)}`} checked={!!it.cn_confirmed} disabled={!canEdit || !it.cn_code}
          onCheckedChange={(v) => confirmation.confirm(() => onPatch({ items: [{ seq: it.seq, cn_confirmed: !!v }] }, `item-${it.seq}`), `${t("customs.item.confirm_cn")}: ${fmtCode(it.cn_code)} → ${t(v ? "review.permission_yes" : "review.permission_no")}`)} />
        <span className={it.cn_confirmed ? "text-foreground" : "text-ink-secondary"}>
          {it.cn_confirmed ? t("customs.item.cn_confirmed") : t("customs.item.confirm_cn")}
        </span>
      </label>
      {confirmation.dialog}
    </div>
  );
}

function DutyCell({ it, start }) {
  const { user } = useAuth();
  const { t, locale } = useT();
  const src = it.duty_rate_pct === null || it.duty_rate_pct === undefined ? "none" : it.duty_source || "ai_estimate";
  return (
    <div className={cn("flex flex-col", start ? "items-start" : "items-end")}>
      <span className="tabular">{money(it.duty_cents, locale, { decimals: 2 })}</span>
      <span className={cn("text-[11px] whitespace-nowrap", src === "none" ? "text-warning-fg" : "text-ink-tertiary")}>
        {src === "none" ? t("customs.duty_source.none") : `${pctFmt(it.duty_rate_pct, locale)}${src === "seed" && user?.org_type !== "internal" ? "" : ` · ${t(`customs.duty_source.${src}`)}`}`}
      </span>
    </div>
  );
}

function VatCell({ it, start }) {
  const { locale } = useT();
  return (
    <div className={cn("flex flex-col", start ? "items-start" : "items-end")}>
      <span className="tabular">{money(it.vat_cents, locale, { decimals: 2 })}</span>
      <span className="text-[11px] text-ink-tertiary whitespace-nowrap">{pctFmt(it.vat_rate_pct, locale)}</span>
    </div>
  );
}

const NUM_LABEL = { net_kg: "customs.item.net", gross_kg: "customs.item.gross", packages: "customs.item.packages",
  duty_rate_pct: "customs.item.duty_rate", vat_rate_pct: "customs.item.vat_rate" };
const CUSTOMER_FIELDS = ["description", "origin_country", "net_kg", "gross_kg", "packages", "package_type"];
const BROKER_FIELDS = [...CUSTOMER_FIELDS, "cn_code", "duty_rate_pct", "vat_rate_pct"];

function toForm(it, fields) {
  const f = {};
  fields.forEach((k) => (f[k] = it[k] === null || it[k] === undefined ? "" : String(it[k])));
  return f;
}

/** Edit form + AI reasoning for one item. */
function ItemEditor({ it, mode, canEdit, onPatch, onReclassify, reclassifying, saveState }) {
  const { t, locale } = useT();
  const fields = mode === "ops" ? BROKER_FIELDS : CUSTOMER_FIELDS;
  const [form, setForm] = useState(() => toForm(it, fields));
  const [err, setErr] = useState(null);
  const orig = toForm(it, fields);
  const origKey = JSON.stringify(orig);
  // Re-sync only when the server values of this item change (after a save or a redraft), not on unrelated reloads.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setForm(JSON.parse(origKey)), [origKey]);
  const dirty = fields.filter((k) => form[k] !== orig[k]);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const save = async () => {
    const p = { seq: it.seq };
    for (const k of dirty) {
      let v = form[k].trim();
      if (NUM_LABEL[k]) {
        const n = parseNum(v);
        if (Number.isNaN(n)) { setErr(`${t(NUM_LABEL[k])}: ${v}`); return; }
        v = n;
      }
      if (k === "origin_country") v = v.toUpperCase();
      if (k === "cn_code") v = v.replace(/\D/g, "");
      p[k] = v === "" ? null : v;
    }
    setErr(null);
    try { await onPatch({ items: [p] }, `item-${it.seq}`); } catch (e) { setErr(e.message); }
  };

  const sug = it.hs_suggestion || {};
  const dis = !canEdit;
  const lbl = "text-xs text-ink-tertiary";
  return (
    <div className="grid gap-4 py-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <div className="flex min-w-0 flex-col gap-2 text-sm">
        {mode === "ops" && (sug.rationale || sug.reason) && (
          <div>
            <p className={lbl}>{t("customs.item.rationale")}</p>
            <p className="mt-0.5 flex gap-1.5"><Sparkles className="mt-0.5 size-3.5 shrink-0 text-[color:var(--status-submitted-fg)]" aria-hidden />
              <span>{sug.rationale || sug.reason}</span></p>
          </div>
        )}
        {mode === "ops" && sug.hs_hint_check?.code && (
          <p className="text-xs text-ink-secondary">
            {t("customs.item.hs_hint", { code: fmtCode(sug.hs_hint_check.code) })} · {sug.hs_hint_check.note}
          </p>
        )}
        {mode === "ops" && sug.cn8_note && <p className="text-xs text-ink-secondary">{displayText(sug.cn8_note, locale)}</p>}
        {(sug.duty?.basis || it.duty_label) && mode === "ops" && (
          <div>
            <p className={lbl}>{t("customs.item.duty_basis")}</p>
            <p className="mt-0.5 text-sm">{[sug.duty?.basis, it.duty_label].filter(Boolean).map((x) => displayText(x, locale)).join(" · ")}</p>
          </div>
        )}
        {it.preference_note && <p className="text-xs text-ink-secondary">{t("customs.item.preference")}: {it.preference_note}</p>}
        {(it.flags || []).filter((f) => f.message).map((f, i) => (
          <p key={i} className="text-xs"><FlagChips flags={[f]} className="mr-1.5 align-middle" />{f.message}</p>
        ))}
        {it.cn_confirmed && it.cn_confirmed_by && (
          <p className="text-xs text-ink-tertiary">{t("customs.item.cn_confirmed_by", { name: it.cn_confirmed_by.name, time: dateTime(it.cn_confirmed_at, locale) })}</p>
        )}
        {mode === "ops" && canEdit && (
          <div className="flex flex-wrap gap-2 pt-1">
            <HsPicker initial={it.description?.split(/[,(]/)[0]?.slice(0, 40) || ""}
              onPick={(h) => onPatch({ items: [{ seq: it.seq, hs_code: h.code }] }, `item-${it.seq}`)} />
            {onReclassify && (
              <Button variant="ghost" size="xs" disabled={reclassifying} onClick={() => onReclassify(it.seq)}>
                <Sparkles /> {reclassifying ? t("customs.item.classifying") : t("customs.item.reclassify")}
              </Button>
            )}
          </div>
        )}
      </div>

      <div className="grid min-w-0 grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-4">
        <label className="col-span-2 sm:col-span-4 flex flex-col gap-1">
          <span className={lbl}>{t("customs.item.description")}</span>
          <Textarea rows={2} value={form.description} onChange={set("description")} disabled={dis} className="min-h-[52px]" />
        </label>
        <label className="flex flex-col gap-1">
          <span className={lbl}>{t("customs.item.origin")}</span>
          <Input value={form.origin_country} onChange={set("origin_country")} disabled={dis} maxLength={2} className="uppercase font-mono" placeholder="CN" />
        </label>
        <label className="flex flex-col gap-1">
          <span className={lbl}>{t("customs.item.packages")}</span>
          <Input inputMode="numeric" value={form.packages} onChange={set("packages")} disabled={dis} className="tabular" />
        </label>
        <label className="flex flex-col gap-1">
          <span className={lbl}>{t("customs.item.net")}</span>
          <Input inputMode="decimal" value={form.net_kg} onChange={set("net_kg")} disabled={dis} className="tabular" />
        </label>
        <label className="flex flex-col gap-1">
          <span className={lbl}>{t("customs.item.gross")}</span>
          <Input inputMode="decimal" value={form.gross_kg} onChange={set("gross_kg")} disabled={dis} className="tabular" />
        </label>
        <label className="col-span-2 flex flex-col gap-1">
          <span className={lbl}>{t("customs.item.package_type")}</span>
          <Select value={form.package_type || undefined} onValueChange={(v) => setForm((f) => ({ ...f, package_type: v }))} disabled={dis}>
            <SelectTrigger><SelectValue placeholder="—" /></SelectTrigger>
            <SelectContent>
              {packageOptions(form.package_type).map((o) => (
                <SelectItem key={o.value} value={o.value}>{o.kind ? t(`customs.pkg.${o.kind}`) : o.value}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        {mode === "ops" && (
          <>
            <label className="col-span-2 flex flex-col gap-1">
              <span className={lbl}>{t("customs.item.cn")}</span>
              <Input value={form.cn_code} onChange={set("cn_code")} disabled={dis} inputMode="numeric" maxLength={10} className="font-mono tabular" />
            </label>
            <label className="flex flex-col gap-1">
              <span className={lbl}>{t("customs.item.duty_rate")}</span>
              <Input inputMode="decimal" value={form.duty_rate_pct} onChange={set("duty_rate_pct")} disabled={dis} className="tabular" />
            </label>
            <label className="flex flex-col gap-1">
              <span className={lbl}>{t("customs.item.vat_rate")}</span>
              <Input inputMode="decimal" value={form.vat_rate_pct} onChange={set("vat_rate_pct")} disabled={dis} className="tabular" />
            </label>
            <p className="col-span-2 sm:col-span-4 -mt-1 text-xs text-ink-tertiary">{t("customs.item.cn_hint")}</p>
          </>
        )}
        {canEdit && (
          <div className="col-span-2 sm:col-span-4 flex items-center gap-3">
            <Button size="sm" onClick={save} disabled={!dirty.length || saveState === "saving"}>{t("action.save")}</Button>
            {dirty.length > 0 && (
              <Button size="sm" variant="ghost" onClick={() => { setForm(orig); setErr(null); }}>{t("action.cancel")}</Button>
            )}
            {err ? <span className="text-xs text-danger-fg">{err}</span> : <SaveState state={saveState} />}
          </div>
        )}
      </div>
    </div>
  );
}

function ItemCard({ it, mode, canEdit, onPatch, open, onToggle, editorProps, highlight }) {
  const { t, locale } = useT();
  return (
    <li id={`item-${it.seq}`} className={cn("py-3", highlight && "bg-hover -mx-2 px-2 rounded-lg")}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs text-ink-tertiary tabular">#{it.seq}</p>
          <p className="text-sm leading-5">{it.description || "—"}</p>
        </div>
        <CodeCell it={it} mode={mode} canEdit={canEdit} onPatch={onPatch} />
      </div>
      <dl className="mt-2 grid grid-cols-3 gap-2 text-sm">
        <div><dt className="text-xs text-ink-tertiary">{t("customs.item.origin")}</dt><dd className="font-mono text-[13px]">{it.origin_country || "—"}</dd></div>
        <div><dt className="text-xs text-ink-tertiary">{t("customs.item.weight")}</dt><dd className="tabular">{num(it.net_kg, locale, 1)} / {num(it.gross_kg, locale, 1)}</dd></div>
        <div><dt className="text-xs text-ink-tertiary">{t("customs.item.packages")}</dt><dd className="tabular">{num(it.packages, locale)}</dd></div>
        <div><dt className="text-xs text-ink-tertiary">{t("customs.totals.customs_value")}</dt><dd className="tabular">{money(it.customs_value_cents, locale, { decimals: 2 })}</dd></div>
        <div><dt className="text-xs text-ink-tertiary">{t("customs.item.duty")}</dt><dd><DutyCell it={it} start /></dd></div>
        <div><dt className="text-xs text-ink-tertiary">{t("customs.item.vat")}</dt><dd><VatCell it={it} start /></dd></div>
      </dl>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
        {mode === "ops" ? <TaricCell it={it} canEdit={canEdit} onPatch={onPatch} /> : <FlagChips flags={it.flags} />}
        <Button variant="ghost" size="xs" onClick={onToggle} aria-expanded={open}>
          {open ? <ChevronDown /> : <ChevronRight />} {open ? t("customs.item.hide") : canEdit ? t("action.edit") : t("customs.item.details")}
        </Button>
      </div>
      {mode === "ops" && <FlagChips flags={it.flags} className="mt-1" />}
      {open && <ItemEditor it={it} mode={mode} canEdit={canEdit} onPatch={onPatch} {...editorProps} />}
    </li>
  );
}

/**
 * decl: full declaration. mode: "ops" | "portal". onPatch(body, key) -> Promise. saveState(key) -> state.
 * focusSeq: item to open/highlight (e.g. from a validation link).
 */
export default function ItemsTable({ decl, mode = "ops", canEdit, onPatch, saveState, onReclassify, reclassifying, focusSeq }) {
  const { t, locale } = useT();
  const narrow = useIsNarrow();
  const items = decl?.items || [];
  const [open, setOpen] = useState(() => new Set());
  useEffect(() => {
    if (!focusSeq) return;
    setOpen((s) => new Set(s).add(focusSeq));
    setTimeout(() => document.getElementById(`item-${focusSeq}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 50);
  }, [focusSeq]);
  const toggle = (seq) => setOpen((s) => { const n = new Set(s); n.has(seq) ? n.delete(seq) : n.add(seq); return n; });
  const editorProps = (it) => ({ onReclassify, reclassifying: reclassifying === it.seq, saveState: saveState?.(`item-${it.seq}`) });
  const tot = decl?.totals || {};

  if (narrow) {
    return (
      <ul className="divide-y divide-[color:var(--border-subtle)]">
        {items.map((it) => (
          <ItemCard key={it.seq} it={it} mode={mode} canEdit={canEdit} onPatch={onPatch} open={open.has(it.seq)}
            onToggle={() => toggle(it.seq)} editorProps={editorProps(it)} highlight={focusSeq === it.seq} />
        ))}
      </ul>
    );
  }

  const ops = mode === "ops";
  const th = "h-8 px-2 text-left align-middle text-xs font-medium text-ink-tertiary whitespace-nowrap";
  const td = "px-2 py-2 align-top";
  const cols = 8;
  return (
    <div className="relative w-full overflow-x-auto">
      <table className="w-full min-w-[820px] border-collapse text-sm">
        <thead>
          <tr className="border-b border-[color:var(--border-subtle)]">
            <th className={cn(th, "w-8")}><span className="sr-only">{t("customs.item.details")}</span></th>
            <th className={cn(th, "w-6")}>{t("customs.item.seq")}</th>
            <th className={th}>{t("customs.item.description")}</th>
            <th className={th}>{t("customs.item.code")}</th>
            <th className={cn(th, "text-right")}>{t("customs.item.weight")} kg</th>
            <th className={cn(th, "text-right")}>{t("customs.totals.customs_value")}</th>
            <th className={cn(th, "text-right")}>{t("customs.item.duty")}</th>
            <th className={cn(th, "text-right")}>{t("customs.item.vat")}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((it) => {
            const isOpen = open.has(it.seq);
            return (
              <React.Fragment key={it.seq}>
                <tr id={`item-${it.seq}`} className={cn("border-b border-[color:var(--border-subtle)] hover:bg-hover",
                  (isOpen || focusSeq === it.seq) && "bg-hover", isOpen && "border-b-0")}>
                  <td className={cn(td, "pr-0")}>
                    <Button variant="ghost" size="icon-xs" onClick={() => toggle(it.seq)} aria-expanded={isOpen}
                      aria-label={isOpen ? t("customs.item.hide") : canEdit ? t("customs.item.edit") : t("customs.item.details")}>
                      {isOpen ? <ChevronDown /> : <ChevronRight />}
                    </Button>
                  </td>
                  <td className={cn(td, "tabular text-ink-tertiary")}>{it.seq}</td>
                  <td className={cn(td, "min-w-[200px] max-w-[320px]")}>
                    <p className="line-clamp-2 leading-5">{it.description || "—"}</p>
                    <p className="text-xs text-ink-tertiary tabular">
                      <span className="font-mono text-foreground/80" title={t("customs.item.origin")}>{it.origin_country || "—"}</span>
                      {it.quantity !== null && it.quantity !== undefined ? ` · ${num(it.quantity, locale, 2)} ${it.unit || ""}` : ""}
                      {` · ${t("customs.item.invoice", { amount: invoiceMoney(it.invoice_amount, it.currency, locale) })}`}
                    </p>
                    <FlagChips flags={it.flags} className="mt-1" />
                  </td>
                  <td className={td}>
                    <div className="flex flex-col items-start gap-1.5">
                      <CodeCell it={it} mode={mode} canEdit={canEdit} onPatch={onPatch} />
                      {ops && <TaricCell it={it} canEdit={canEdit} onPatch={onPatch} />}
                    </div>
                  </td>
                  <td className={cn(td, "text-right tabular whitespace-nowrap")}>
                    {num(it.net_kg, locale, 1)} / {num(it.gross_kg, locale, 1)}
                    <span className="block text-[11px] text-ink-tertiary">{num(it.packages, locale)} {it.package_type ? tEnum(t,"package_type",it.package_type) : t("customs.item.packages").toLowerCase()}</span>
                  </td>
                  <td className={cn(td, "text-right tabular")}>
                    {money(it.customs_value_cents, locale, { decimals: 2 })}
                    <span className="block text-[11px] text-ink-tertiary" title={t("customs.item.value")}>{money(it.value_cents, locale, { decimals: 2 })}</span>
                  </td>
                  <td className={td}><DutyCell it={it} /></td>
                  <td className={td}><VatCell it={it} /></td>
                </tr>
                {isOpen && (
                  <tr className="border-b border-[color:var(--border-subtle)] bg-hover">
                    <td />
                    <td colSpan={cols - 1} className="px-2 pb-3">
                      <ItemEditor it={it} mode={mode} canEdit={canEdit} onPatch={onPatch} {...editorProps(it)} />
                    </td>
                  </tr>
                )}
              </React.Fragment>
            );
          })}
        </tbody>
        <tfoot>
          <tr className="border-t border-[color:var(--border-default)] font-medium">
            <td />
            <td colSpan={3} className="px-2 py-2 text-sm">{t("customs.item.totals")}</td>
            <td className="px-2 py-2 text-right tabular whitespace-nowrap">
              {num(tot.net_kg, locale, 1)} / {num(tot.gross_kg, locale, 1)}
              <span className="block text-[11px] font-normal text-ink-tertiary">{t("customs.item.packages_count", { n: num(tot.packages, locale) })}</span>
            </td>
            <td className="px-2 py-2 text-right tabular">{money(tot.customs_value_cents, locale, { decimals: 2 })}</td>
            <td className="px-2 py-2 text-right tabular">{money(tot.duty_cents, locale, { decimals: 2 })}</td>
            <td className="px-2 py-2 text-right tabular">{money(tot.vat_cents, locale, { decimals: 2 })}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
