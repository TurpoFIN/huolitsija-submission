// Customer claims. Decision: which claims are open or near the one-year time bar, and filing a new one
// with the liability limit visible before sending.
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Metric, PageHeader } from "@/components/app/layout";
import { Segmented } from "@/components/app/FilterBar";
import { EmptyState, Freshness, Loadable, LoadingRows } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { money } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { SplitView, useMedia, useWide } from "@/features/exceptions/common";
import ClaimDetail from "@/features/claims/ClaimDetail";
import ClaimForm from "@/features/claims/ClaimForm";
import ClaimsList from "@/features/claims/ClaimsList";

const FILTERS = ["all", "open", "draft", "approved", "rejected", "paid"];

function FormPane({ title, onClose, children }) {
  const { t } = useT();
  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-md font-medium">{title}</h2>
        <Button variant="ghost" size="icon-xs" onClick={onClose} aria-label={t("claims.d.close")}><X /></Button>
      </div>
      {children}
    </div>
  );
}

export default function Claims() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const wide = useWide();
  const narrow = useMedia("(max-width: 767px)");
  const status = FILTERS.includes(params.get("status")) ? params.get("status") : "all";
  const selected = params.get("id");
  const isNew = params.get("new") === "1";
  const presetShipment = params.get("shipment_id");
  const [editing, setEditing] = useState(null);
  const [saved, setSaved] = useState(null); // {id, label}: inline confirmation after a save
  const canFile = can("company.book") || can("company.manage");

  const setParam = useCallback((changes, replace = false) => {
    setParams((p) => {
      const n = new URLSearchParams(p);
      Object.entries(changes).forEach(([k, v]) => (v === null || v === undefined || v === "" ? n.delete(k) : n.set(k, v)));
      return n;
    }, { replace });
  }, [setParams]);

  const q = useApi(`/claims?status=${status}`, { pollMs: 60000 });
  const counts = q.data?.counts || {};
  const items = useMemo(() => q.data?.items || [], [q.data]);
  const openCount = (counts.submitted || 0) + (counts.in_review || 0);

  useEffect(() => {
    if (wide && !selected && !isNew && !editing && items.length) setParam({ id: items[0].id }, true);
  }, [wide, selected, isNew, editing, items, setParam]);

  const openNew = () => { setEditing(null); setParam({ new: "1", id: null }); };
  const closePane = () => { setEditing(null); setParam({ new: null, id: null, shipment_id: null }); };
  const afterSave = (claim, { submitted } = {}) => {
    setEditing(null);
    setSaved({ id: claim.id, label: submitted ? t("claims.form.submitted") : t("claims.form.saved") });
    q.reload();
    setParam({ new: null, shipment_id: null, id: claim.id });
  };

  const countFor = (f) => (f === "all" ? Object.values(counts).reduce((a, b) => a + (b || 0), 0) : f === "open" ? openCount : counts[f] || 0);

  const meta = q.data ? (
    <>
      <Metric value={openCount} label={t("claims.meta.open")} to="/portal/claims?status=open" />
      <span className="text-ink-tertiary">·</span>
      <Metric value={money(q.data.open_claimed_cents || 0, locale, { decimals: 2 })} label={t("claims.meta.claimed")} />
      <span className="text-ink-tertiary">·</span>
      <Metric value={q.data.time_bar_alerts || 0} label={t("claims.meta.alerts")} tone={q.data.time_bar_alerts ? "warning" : undefined} />
      {counts.draft > 0 && (
        <>
          <span className="text-ink-tertiary">·</span>
          <Metric value={counts.draft} label={t("claims.meta.drafts")} tone="warning" to="/portal/claims?status=draft" />
        </>
      )}
    </>
  ) : null;

  const newButton = (
    <Button size="sm" onClick={openNew} disabled={!canFile} title={!canFile ? t("claims.form.no_perm") : undefined}><Plus /> {t("claims.new")}</Button>
  );

  const list = (
    <Loadable q={q} skeleton={<LoadingRows rows={5} />} isEmpty={(d) => d.items.length === 0}
      empty={status === "all"
        ? <EmptyState title={t("claims.empty.portal")} body={t("claims.empty.portal_body")}
            action={!canFile ? <p className="text-xs text-ink-tertiary">{t("claims.form.no_perm")}</p> : wide ? null : newButton} />
        : <EmptyState title={t("claims.empty.filtered")} action={<Button size="sm" variant="outline" onClick={() => setParam({ status: null })}>{t("claims.filter.all")}</Button>} />}>
      {(d) => <ClaimsList rows={d.items} selected={selected} onOpen={(id) => { setEditing(null); setParam({ id, new: null }); }} narrow={narrow} />}
    </Loadable>
  );

  const noClaims = !!q.data && status === "all" && items.length === 0;
  let pane = null;
  if (editing) {
    pane = (
      <FormPane title={t("claims.form.title_edit", { number: editing.number })} onClose={() => setEditing(null)}>
        <ClaimForm key={editing.id} initial={editing} onSaved={afterSave} onCancel={() => setEditing(null)} canFile={canFile} />
      </FormPane>
    );
  } else if (isNew) {
    pane = (
      <FormPane title={t("claims.form.title_new")} onClose={closePane}>
        <ClaimForm presetShipmentId={presetShipment} onSaved={afterSave} onCancel={closePane} canFile={canFile} />
      </FormPane>
    );
  } else if (selected) {
    pane = <ClaimDetail key={selected} id={selected} mode="customer" onChanged={q.reload} onEdit={(c) => { setSaved(null); setEditing(c); }}
      savedLabel={saved?.id === selected ? saved.label : null} onClose={wide ? null : closePane} />;
  }

  return (
    <div>
      <PageHeader
        title={t("claims.title")}
        meta={meta}
        actions={<><Freshness at={q.updatedAt} onRefresh={q.reload} refreshing={q.refreshing} />{newButton}</>}
      />
      <div className="mb-3">
        <Segmented value={status} onChange={(v) => setParam({ status: v === "all" ? null : v })} label={t("claims.filter.status")}
          options={FILTERS.map((f) => ({ value: f, label: t(`claims.filter.${f}`), count: q.data ? countFor(f) : undefined }))} />
      </div>
      <SplitView wide={wide} open={!!pane} onClose={closePane} title={t("claims.title")} list={list}
        detail={pane || (
          <div className="flex flex-col items-start gap-3 p-4">
            <p className="text-sm text-ink-tertiary">{items.length ? t("claims.select") : t("claims.empty.portal_body")}</p>
            {canFile && !items.length && newButton}
          </div>
        )} />
    </div>
  );
}
