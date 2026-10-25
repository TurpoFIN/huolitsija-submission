// One tender: every lane with its price, firmness and warnings; then price, return the filled file, record the award.
import React, { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { AlertTriangle, Award, Calculator, Download, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { KV, PageHeader } from "@/components/app/layout";
import { Freshness } from "@/components/app/states";
import { useConfirmChange } from "@/components/app/ConfirmChange";
import { apiPost, fileUrl, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, num } from "@/lib/format";
import { displayText } from "@/lib/localizedData";
import { useT } from "@/lib/i18n";
import { ModeLabel, useIsNarrow } from "@/features/quotes/ops/common";
import { warningText } from "@/features/quotes/quoteFormat";
import { Chip, ComBadge, ComLoadable, Eur, NeedsLine, Place, ScrollTable, mutationError, useLabel } from "./common";
import { tender as normTender } from "./shapes";

export default function TenderDetail() {
  const { id } = useParams();
  const q = useApi(`/tenders/${id}`, { pollMs: 15000 });
  return (
    <ComLoadable q={q} perm="quotes.write">
      {(d) => <Body tn={normTender(d.tender || d)} q={q} />}
    </ComLoadable>
  );
}

function Body({ tn, q }) {
  const { t, locale } = useT();
  // tender warnings carry message_fi; warnings copied from the pricing engine carry a code with a quotes.warn.* text
  const warnText = (w) => (locale === "fi" && w.message_fi ? w.message_fi
    : w.code && t(`quotes.warn.${w.code}`) !== `quotes.warn.${w.code}` ? warningText(t, w)
      : displayText(w.message || w.text || w.en || w.fi || w.code, locale));
  const label = useLabel();
  const navigate = useNavigate();
  const narrow = useIsNarrow();
  const { can } = useAuth();
  const canWrite = can("quotes.write");
  const { confirm, dialog } = useConfirmChange();
  const [busy, setBusy] = useState(null);
  const run = async (key, path, ok, after) => {
    setBusy(key);
    try {
      const r = await apiPost(path, {});
      toast.success(ok);
      q.reload();
      after?.(r);
    } catch (e) {
      toast.error(mutationError(e, t, locale));
    } finally {
      setBusy(null);
    }
  };
  const unpriced = tn.lanes.filter((l) => l.price_per_unit_cents === null || l.price_per_unit_cents === undefined).length;
  const warned = tn.lanes.filter((l) => l.warnings.length).length;
  const priced = ["priced", "returned", "submitted", "awarded"].includes(tn.status);
  const award = () => confirm(() => run("award", `/tenders/${tn.id}/award`, t("com.tender.awarded"), (r) => {
    const tid = r?.tariff_id || r?.tariff?.id || r?.id;
    if (tid) navigate(`/ops/commercial/tariffs/${tid}`);
  }), t("com.tender.award_q"));

  return (
    <div>
      <PageHeader
        crumbs={[{ label: t("com.tab.tenders"), to: "/ops/commercial/tenders" }, { label: tn.name }]}
        title={tn.name}
        meta={<>
          <ComBadge group="tender" value={tn.status} />
          {tn.customer && <span>{tn.customer}</span>}
          {tn.due_at && <span>{t("com.tender.due")}: {date(tn.due_at, locale)}</span>}
          <Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />
        </>}
        actions={canWrite && <>
          <Button size="sm" variant={priced ? "outline" : "default"} disabled={!!busy || tn.status === "pricing" || tn.status === "awarded"}
            onClick={() => run("price", `/tenders/${tn.id}/price`, t("com.tender.priced"))}>
            <Calculator />{busy === "price" || tn.status === "pricing" ? t("com.tender.pricing") : priced ? t("com.tender.reprice") : t("com.tender.price")}
          </Button>
          {priced ? (
            <Button size="sm" variant="outline" asChild><a href={fileUrl(`/tenders/${tn.id}/file`)} download><Download />{t("com.tender.download")}</a></Button>
          ) : (
            <Button size="sm" variant="outline" disabled><Download />{t("com.tender.download")}</Button>
          )}
          <Button size="sm" variant={tn.status === "priced" ? "default" : "outline"} disabled={!!busy || !priced || tn.status === "awarded"}
            onClick={() => run("return", `/tenders/${tn.id}/return`, t("com.tender.returned"))}><Send />{t("com.tender.return")}</Button>
          <Button size="sm" variant="outline" disabled={!!busy || !priced || tn.status === "awarded"} onClick={award}><Award />{t("com.tender.award")}</Button>
        </>}
      />

      <NeedsLine items={[
        unpriced > 0 && { tone: "warning", icon: AlertTriangle, text: t("com.tender.unpriced_n", { n: num(unpriced, locale) }) },
        warned > 0 && { tone: "warning", text: t("com.tender.warned_n", { n: num(warned, locale) }) },
        tn.status === "awarded" && tn.tariff_id && { text: t("com.tender.became_tariff"), onClick: () => navigate(`/ops/commercial/tariffs/${tn.tariff_id}`) },
      ]} />

      <KV cols={3} className="mb-5" items={[
        { label: t("com.tender.lanes"), value: `${num(tn.priced_count, locale)} / ${num(tn.lane_count, locale)}`, hint: t("com.tender.priced_short") },
        { label: t("com.tender.annual_value"), value: tn.total_annual_cents !== null ? <Eur cents={tn.total_annual_cents} /> : null },
        { label: t("com.tender.file"), value: tn.file_name },
      ]} />

      {tn.lanes.length === 0 ? <p className="text-sm text-ink-tertiary">{t("com.tender.no_lanes")}</p> : narrow ? (
        <ul className="divide-y divide-[color:var(--border-subtle)] border-y">
          {tn.lanes.map((l, i) => (
            <li key={l.id || i} className="flex flex-col gap-1 px-1 py-2.5">
              <span className="flex items-start justify-between gap-3">
                <span className="flex items-center gap-2 text-sm"><Place loc={l.origin} /><span className="text-ink-tertiary">→</span><Place loc={l.destination} /></span>
                <Eur cents={l.annual_value_cents} className="text-sm" />
              </span>
              <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-secondary">
                <ModeLabel mode={l.mode} />
                <span>{num(l.annual_volume, locale)} {l.volume_unit ? label("unit", l.volume_unit) : ""}</span>
                <span><Eur cents={l.price_per_unit_cents} decimals={2} /> / {l.price_unit ? label("unit", l.price_unit) : t("com.unit")}</span>
                {l.firmness && <Chip>{label("firm", l.firmness)}</Chip>}
              </span>
              {l.warnings.map((w, j) => <span key={j} className="text-xs text-warning-fg">{warnText(w)}</span>)}
            </li>
          ))}
        </ul>
      ) : (
        <ScrollTable label={t("com.tender.lanes")}>
          <table className="w-full text-sm">
            <thead className="text-xs text-ink-tertiary">
              <tr className="border-b border-[color:var(--border-subtle)]">
                <th className="px-2 py-2 text-left font-medium">{t("field.origin")}</th>
                <th className="px-2 py-2 text-left font-medium">{t("field.destination")}</th>
                <th className="px-2 py-2 text-left font-medium">{t("field.mode")}</th>
                <th className="px-2 py-2 text-right font-medium">{t("com.tender.volume")}</th>
                <th className="px-2 py-2 text-right font-medium">{t("com.tender.unit_price")}</th>
                <th className="px-2 py-2 text-right font-medium">{t("com.tender.annual_value")}</th>
                <th className="px-2 py-2 text-left font-medium">{t("com.tender.firmness")}</th>
                <th className="px-2 py-2 text-left font-medium">{t("com.tender.warnings")}</th>
              </tr>
            </thead>
            <tbody>
              {tn.lanes.map((l, i) => (
                <tr key={l.id || i} className="border-b border-[color:var(--border-subtle)] align-top">
                  <td className="px-2 py-2"><Place loc={l.origin} /></td>
                  <td className="px-2 py-2"><Place loc={l.destination} /></td>
                  <td className="px-2 py-2"><ModeLabel mode={l.mode} service={l.service} /></td>
                  <td className="px-2 py-2 text-right tabular whitespace-nowrap">{num(l.annual_volume, locale)} <span className="text-xs text-ink-tertiary">{l.volume_unit ? label("unit", l.volume_unit) : ""}</span></td>
                  <td className="px-2 py-2 text-right whitespace-nowrap"><Eur cents={l.price_per_unit_cents} decimals={2} />{l.price_unit && <span className="text-xs text-ink-tertiary"> / {label("unit", l.price_unit)}</span>}</td>
                  <td className="px-2 py-2 text-right"><Eur cents={l.annual_value_cents} /></td>
                  <td className="px-2 py-2">{l.firmness ? <Chip>{label("firm", l.firmness)}</Chip> : <span className="text-ink-tertiary">—</span>}</td>
                  <td className="px-2 py-2 max-w-[280px]">
                    {l.warnings.length === 0 ? <span className="text-ink-tertiary">—</span> : l.warnings.map((w, j) => (
                      <span key={j} className="block text-xs text-warning-fg">{warnText(w)}</span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollTable>
      )}
      {dialog}
    </div>
  );
}
