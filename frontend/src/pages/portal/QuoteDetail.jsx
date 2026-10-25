// Portal: one quote. Decision: "which option do I book?" -> ranked options, breakdown, sticky total + CTA with validity.
import React, { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowRight, ChevronDown, Eye, FileDown, Loader2, Pencil, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader, Section } from "@/components/app/layout";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { ErrorState, Loadable } from "@/components/app/states";
import { apiPost, fileUrl, useApi } from "@/lib/api";
import { date, dateTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import SourcingStatus from "@/features/quotes/SourcingStatus";
import QuoteOptions from "@/features/quotes/QuoteOptions";
import WarningsList from "@/features/quotes/WarningsList";
import RouteText from "@/features/quotes/RouteText";
import BookingBlockers, { ACCOUNT_BLOCKERS } from "@/features/quotes/BookingBlockers";
import { cargoSummary, eur, isExpired, lineText, locName, modeIcon, otherPartyInfo, validityLabel } from "@/features/quotes/quoteFormat";

function DetailSkeleton() {
  return (
    <div aria-busy="true" className="flex flex-col gap-3">
      <Skeleton className="h-4 w-40" />
      <Skeleton className="h-7 w-64" />
      <Skeleton className="h-4 w-3/4" />
      <div className="mt-4 grid gap-6 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="flex flex-col gap-2">
          {[0, 1, 2].map((i) => <Skeleton key={i} className="h-16 w-full rounded-xl" />)}
        </div>
        <Skeleton className="hidden h-64 w-full rounded-xl xl:block" />
      </div>
    </div>
  );
}

function PriceCard({ quote, option, onBook, busy, blockers, bookDisabledReason, onRequote, requoting, hideCta }) {
  const { t, locale } = useT();
  const req = quote.request || {};
  const expired = quote.status === "expired" || isExpired(quote.valid_until);
  const notIncluded = option?.not_included || [];
  return (
    <div className="flex flex-col gap-3 rounded-xl bg-surface px-4 py-3 shadow-[inset_0_0_0_0.5px_var(--border-default)]">
      {option ? (
        <>
          <div className="min-w-0">
            <p className="text-xs text-ink-tertiary">{t("pq.card.selected")}</p>
            <p className="truncate text-sm font-medium">{option.carrier_name || "—"}</p>
            <p className="truncate text-xs text-ink-tertiary">{option.service_name || option.service}</p>
          </div>
          <dl className="flex flex-col gap-1 text-sm">
            <div className="flex items-baseline justify-between gap-3">
              <dt>{t("quotes.bd.total_excl")}</dt>
              <dd className="text-lg tabular whitespace-nowrap">{eur(option.total_cents, locale)}</dd>
            </div>
            <div className="flex items-baseline justify-between gap-3 text-xs text-ink-tertiary">
              <dt>{req.incoterm ? `${req.incoterm}${req.incoterm_place ? ` ${req.incoterm_place}` : ""}` : t("pq.card.no_incoterm")}</dt>
              <dd className="tabular">{t("quotes.bd.vat_est")} {eur(option.vat_cents, locale)}</dd>
            </div>
          </dl>
          {otherPartyInfo(option).partial && (
            <p className="text-xs text-warning-fg">{t("quotes.share.note", { incoterm: req.incoterm || "" })}</p>
          )}
          {notIncluded.length > 0 && (
            <div className="text-xs">
              <p className="text-ink-secondary">{t("quotes.bd.not_included")}</p>
              <ul className="mt-0.5 flex flex-col gap-0.5 text-ink-tertiary">
                {notIncluded.slice(0, 5).map((n, i) => (
                  <li key={i} className="flex justify-between gap-3">
                    <span className="min-w-0">{lineText(n, locale)}</span>
                    {n.amount_cents !== null && n.amount_cents !== undefined && <span className="tabular whitespace-nowrap">≈ {eur(n.amount_cents, locale)}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      ) : (
        <p className="text-sm text-ink-tertiary">{t("pq.card.none")}</p>
      )}
      <p className={cn("text-xs", expired ? "text-danger-fg" : "text-ink-secondary")}>
        {t("pq.card.rate_expires", { date: date(option?.valid_until || quote.valid_until, locale) })}
        <span className="text-ink-tertiary"> · {validityLabel(t, option?.valid_until || quote.valid_until)}</span>
      </p>
      <BookingBlockers items={blockers} company={quote.company} />
      {expired ? (
        <Button onClick={onRequote} disabled={requoting}>
          {requoting ? <Loader2 className="animate-spin" /> : <RefreshCw />} {t("pq.detail.requote")}
        </Button>
      ) : option && !hideCta ? (
        <>
          <Button onClick={() => onBook(option)} disabled={!!bookDisabledReason || busy}>
            {busy && <Loader2 className="animate-spin" />}
            {t("pq.card.book", { amount: eur(option.total_cents, locale) })}
          </Button>
          {bookDisabledReason && <p className="text-xs text-ink-tertiary">{bookDisabledReason}</p>}
        </>
      ) : null}
      <p className="text-xs text-ink-tertiary">
        {t("pq.terms.prefix")}{" "}
        <Link to="/terms" className="underline underline-offset-2 hover:text-foreground">{quote.terms_version || "NSAB 2015 (PSYM 2015)"}</Link>
      </p>
    </div>
  );
}

function approvalReasons(t, approval) {
  const rs = approval?.reasons?.length ? approval.reasons : approval?.reason ? [approval.reason] : [];
  if (!rs.length) return "";
  const txt = rs.map((r) => {
    const k = `pq.approval.${r}`;
    const v = t(k);
    return v === k ? r.replace(/_/g, " ") : v;
  });
  return ` (${txt.join(", ")})`;
}

function StatusLine({ quote }) {
  const { t, locale } = useT();
  const s = quote.status;
  if (s === "pending_approval") {
    return (
      <p className="mb-4 flex items-start gap-2 rounded-lg px-3 py-2 text-sm status-in-review">
        <Eye className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span>{t("pq.detail.review", { reason: approvalReasons(t, quote.approval) })}</span>
      </p>
    );
  }
  if (s === "booked" && quote.shipment_id) {
    return (
      <p className="mb-4 flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge group="quote" value="booked" size="sm" />
        <span>{t("pq.detail.booked_as")}</span>
        <Link to={`/portal/shipments/${quote.shipment_id}`} className="inline-flex items-center gap-1 font-medium underline underline-offset-2">
          {quote.shipment_reference || t("pq.detail.open_shipment")} <ArrowRight className="size-3.5" aria-hidden />
        </Link>
      </p>
    );
  }
  if (s === "rejected") return <p className="mb-4 text-sm text-ink-secondary">{t("pq.detail.rejected")}</p>;
  if (s === "expired" || isExpired(quote.valid_until)) {
    return <p className="mb-4 text-sm text-ink-secondary">{t("pq.detail.expired", { date: date(quote.valid_until, locale) })}</p>;
  }
  if (s === "accepted" && quote.accepted_option_id) {
    return (
      <p className="mb-4 flex flex-wrap items-center gap-2 text-sm">
        <span>{t("pq.detail.accepted")}</span>
        <Link to={`/portal/bookings/new?quote=${quote.id}&option=${quote.accepted_option_id}`} className="inline-flex items-center gap-1 font-medium underline underline-offset-2">
          {t("pq.detail.continue_booking")} <ArrowRight className="size-3.5" aria-hidden />
        </Link>
      </p>
    );
  }
  return null;
}

function QuoteBody({ data, reload, setData }) {
  const { t, locale } = useT();
  const navigate = useNavigate();
  const quote = data.quote;
  const req = quote.request || {};
  const options = useMemo(() => quote.options || [], [quote.options]);
  const initial = quote.accepted_option_id || options.find((o) => o.recommended)?.id || options[0]?.id || null;
  const [selId, setSelId] = useState(initial);
  useEffect(() => {
    if (!options.find((o) => o.id === selId)) setSelId(initial);
  }, [options, selId, initial]);
  const selected = options.find((o) => o.id === selId) || null;
  const [busy, setBusy] = useState(false);
  const [blockers, setBlockers] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [requoting, setRequoting] = useState(false);
  // the full breakdown starts open only where it sits beside the list; on narrow screens the options come first
  const [wide] = useState(() => typeof window !== "undefined" && window.matchMedia?.("(min-width: 1280px)").matches);
  const shownBlockers = blockers ?? quote.booking_blockers ?? data.booking_blockers ?? [];
  const expired = quote.status === "expired" || isExpired(quote.valid_until);

  let bookDisabledReason = null;
  if (quote.status === "pending_approval") bookDisabledReason = t("pq.reason.review");
  else if (quote.status === "booked") bookDisabledReason = t("pq.reason.booked");
  else if (quote.status === "rejected") bookDisabledReason = t("pq.reason.rejected");
  else if (quote.status === "draft") bookDisabledReason = t("pq.reason.draft");
  else if (shownBlockers.some((b) => ACCOUNT_BLOCKERS.includes(b.code))) bookDisabledReason = t("pq.reason.blockers");

  const book = async (opt) => {
    setBusy(true);
    setActionError(null);
    try {
      const r = await apiPost(`/quotes/${quote.id}/accept`, { option_id: opt.id });
      const b = r.booking_blockers ?? r.quote?.booking_blockers ?? [];
      if (r.quote) setData((d) => ({ ...d, quote: { ...r.quote, booking_blockers: b } }));
      if (b.length) {
        setBlockers(b);
        setBusy(false);
        return;
      }
      navigate(`/portal/bookings/new?quote=${quote.id}&option=${opt.id}`);
    } catch (e) {
      setActionError(e.status === 0 ? t("state.offline") : e.message);
      setBusy(false);
    }
  };

  const requote = async () => {
    setRequoting(true);
    setActionError(null);
    try {
      const r = await apiPost(`/quotes/${quote.id}/requote`, {});
      if (r.quote?.id && r.quote.id !== quote.id) navigate(`/portal/quotes/${r.quote.id}`);
      else {
        setData((d) => ({ ...d, quote: r.quote }));
        setBlockers(null);
      }
    } catch (e) {
      setActionError(e.status === 0 ? t("state.offline") : e.message);
    } finally {
      setRequoting(false);
    }
  };

  const ModeIcon = modeIcon(req.mode);
  const notQuoted = quote.not_quoted || [];
  const priced = quote.updated_at || quote.created_at;

  const showBar = !!selected && !expired && quote.status !== "booked";
  const card = (narrow) => (
    <PriceCard quote={quote} option={selected} onBook={book} busy={busy} blockers={shownBlockers} hideCta={narrow && showBar}
      bookDisabledReason={bookDisabledReason} onRequote={requote} requoting={requoting} />
  );

  return (
    <>
      <PageHeader
        crumbs={[{ label: t("nav.quotes"), to: "/portal/quotes" }, { label: quote.number || t("pq.detail.untitled") }]}
        title={
          <span className="inline-flex flex-wrap items-center gap-2">
            <span>{quote.number || t("pq.detail.untitled")}</span>
            <StatusBadge group="quote" value={expired && quote.status === "sent" ? "expired" : quote.status} />
          </span>
        }
        meta={
          <>
            <RouteText origin={req.origin} destination={req.destination} className="text-foreground" />
            <span className="hidden text-ink-tertiary sm:inline">·</span>
            <span className={cn("tabular", expired ? "text-danger-fg" : "")}>
              {t("pq.valid_until", { date: date(quote.valid_until, locale) })}
              <span className="text-ink-tertiary"> ({validityLabel(t, quote.valid_until)})</span>
            </span>
          </>
        }
        actions={
          <>
            <Button variant="outline" asChild>
              <a href={fileUrl(`/quotes/${quote.id}/pdf?lang=${locale}`)} target="_blank" rel="noopener noreferrer">
                <FileDown /> {t("pq.detail.pdf")}
              </a>
            </Button>
            <Button variant="outline" asChild>
              <Link to={`/portal/quotes/new?from=${quote.id}`}><Pencil /> {t("pq.detail.edit")}</Link>
            </Button>
            {expired && (
              <Button onClick={requote} disabled={requoting}>
                {requoting ? <Loader2 className="animate-spin" /> : <RefreshCw />} {t("pq.detail.requote")}
              </Button>
            )}
          </>
        }
      />

      {/* the query in one line */}
      <dl className="mb-4 flex flex-wrap gap-x-5 gap-y-1.5 text-sm">
        <div className="flex items-center gap-1.5">
          <dt className="sr-only">{t("field.mode")}</dt>
          <ModeIcon className="size-4 text-ink-tertiary" aria-hidden />
          <dd>{req.mode ? tEnum(t, "mode", req.mode) : t("pq.detail.all_modes")}</dd>
        </div>
        <div className="flex gap-1.5"><dt className="text-ink-tertiary">{t("pq.col.cargo")}</dt><dd className="tabular">{cargoSummary(req.cargo, t, locale)}</dd></div>
        <div className="flex gap-1.5">
          <dt className="text-ink-tertiary">{t("field.incoterm")}</dt>
          <dd>{req.incoterm ? `${req.incoterm} ${req.incoterm_place || ""}`.trim() : <span className="text-ink-tertiary">—</span>}</dd>
        </div>
        <div className="flex gap-1.5"><dt className="text-ink-tertiary">{t("pq.field.ready_date")}</dt><dd className="tabular">{date(req.ready_date, locale)}</dd></div>
        <div className="flex gap-1.5">
          <dt className="text-ink-tertiary">{t("pq.detail.services")}</dt>
          <dd>
            {[req.customs_clearance ? t("pq.detail.customs_yes") : null,
              req.insurance?.cover && req.insurance.cover !== "NONE" ? t(`quotes.insurance.${req.insurance.cover}`) : null]
              .filter(Boolean).join(" · ") || <span className="text-ink-tertiary">—</span>}
          </dd>
        </div>
        {req.direction && <div className="flex gap-1.5"><dt className="text-ink-tertiary">{t("pq.detail.direction")}</dt><dd>{t(`pq.direction.${req.direction}`)}</dd></div>}
      </dl>

      <StatusLine quote={quote} />
      {quote.ai?.summary && (
        <p className="mb-4 flex items-start gap-2 text-sm text-ink-secondary"><AgentMark className="mt-0.5" /> <span>{quote.ai.summary}</span></p>
      )}
      {actionError && <ErrorState className="mb-4" error={{ message: actionError }} />}
      {/* warnings that change the decision go above the options; info notes follow the list */}
      <SourcingStatus quote={quote} />
      <WarningsList className="mb-4" warnings={(quote.warnings || []).filter((w) => w.level === "warning" || w.level === "error")} />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0">
          <Section title={t("pq.detail.options", { n: options.length })}
            actions={<span className="text-xs text-ink-tertiary">{t("pq.detail.sorted")}</span>}>
            <QuoteOptions
              options={options}
              onSelect={(o) => setSelId(o.id)}
              selectLabel={t("pq.detail.choose")}
              selectedId={selId}
              incoterm={req.incoterm}
              incotermPlace={req.incoterm_place}
              validUntil={quote.valid_until}
              pricedAt={priced}
              defaultOpenId={wide ? initial : undefined}
              emptyText={quote.status === "pending_approval" ? t("pq.detail.options_review") : undefined}
            />
          </Section>
          <WarningsList className="-mt-3 mb-4" dense warnings={(quote.warnings || []).filter((w) => w.level !== "warning" && w.level !== "error")} />
          {notQuoted.length > 0 && (
            <Collapsible className="mb-6">
              <CollapsibleTrigger className="group inline-flex items-center gap-1 text-sm text-ink-secondary hover:text-foreground">
                <ChevronDown className="size-4 transition-transform group-data-[state=open]:rotate-180" aria-hidden />
                {t("pq.detail.not_quoted", { n: notQuoted.length })}
              </CollapsibleTrigger>
              <CollapsibleContent>
                <ul className="mt-2 flex flex-col divide-y divide-[color:var(--border-subtle)] text-sm">
                  {notQuoted.map((n, i) => (
                    <li key={i} className="grid gap-x-4 py-1.5 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)]">
                      <span className="truncate">{n.carrier_name || "—"}{n.mode ? <span className="text-ink-tertiary"> · {tEnum(t, "mode", n.mode)}</span> : null}</span>
                      <span className="text-ink-tertiary">{[n.service_name, n.reason].filter(Boolean).join(" — ")}</span>
                    </li>
                  ))}
                </ul>
              </CollapsibleContent>
            </Collapsible>
          )}
          <p className="text-xs text-ink-tertiary">
            {t("pq.detail.created", { date: dateTime(quote.created_at, locale) })}
            {quote.requested_by?.name ? ` · ${quote.requested_by.name}` : ""}
            {quote.revision ? ` · ${t("pq.detail.revision", { n: quote.revision })}` : ""}
          </p>
        </div>
        <aside className="hidden xl:block" aria-label={t("pq.card.title")}>
          <div className="sticky top-[4.5rem]">{card(false)}</div>
        </aside>
      </div>

      {/* < 1280 px: the card follows the list, and a sticky bar keeps total + CTA in reach */}
      <div className="mt-4 xl:hidden">{card(true)}</div>
      {showBar && (
        <div data-sticky-bar className="sticky bottom-0 z-20 -mx-4 mt-4 flex items-center justify-between gap-3 border-t border-[color:var(--border-subtle)] bg-[var(--bg-canvas)] px-4 py-2.5 xl:hidden">
          <div className="min-w-0">
            <p className="truncate text-xs text-ink-tertiary">{bookDisabledReason || `${selected.carrier_name || "—"} · ${locName(req.destination)}`}</p>
            <p className="text-sm tabular">
              {eur(selected.total_cents, locale)} <span className="text-xs text-ink-tertiary">{t("quotes.opt.excl_vat")}{req.incoterm ? ` · ${req.incoterm}` : ""}</span>
            </p>
          </div>
          <Button onClick={() => book(selected)} disabled={!!bookDisabledReason || busy} aria-describedby={bookDisabledReason ? "book-reason-m" : undefined}>
            {busy && <Loader2 className="animate-spin" />} {t("pq.card.book_short")}
          </Button>
          {bookDisabledReason && <span id="book-reason-m" className="sr-only">{bookDisabledReason}</span>}
        </div>
      )}
    </>
  );
}

export default function QuoteDetail() {
  const { id } = useParams();
  const q = useApi(`/quotes/${id}`);
  return (
    <div>
      <Loadable q={q} skeleton={<DetailSkeleton />}>
        {(d) => <QuoteBody data={d} reload={q.reload} setData={q.setData} />}
      </Loadable>
    </div>
  );
}
