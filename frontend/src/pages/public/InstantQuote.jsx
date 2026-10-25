// Public instant quote (V2): route + cargo in one row -> POST /api/public/quote -> ranked options with breakdown.
// The result survives reload via ?id=&token= (GET /api/public/quote/{id}?token=). "Book this" claims the quote for a
// signed-in customer, or stores the claim token in localStorage "huo.claim" and sends the visitor to sign-up.
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowRight, ChevronDown, FileDown, Info, Mail, RefreshCw, Route } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/app/states";
import { apiGet, apiPost, fileUrl } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { date as fmtDate } from "@/lib/format";
import { cn } from "@/lib/utils";
import SourcingStatus from "@/features/quotes/SourcingStatus";
import QuoteOptions from "@/features/quotes/QuoteOptions";
import WarningsList from "@/features/quotes/WarningsList";
import RouteText from "@/features/quotes/RouteText";
import { isExpired, isIndicative } from "@/features/quotes/quoteFormat";
import { PublicLayout, CONTACT_EMAIL } from "@/features/public/PublicLayout";
import QuoteForm, { pubCargoSummary } from "@/features/public/QuoteForm";
import { emptyForm, formFromParams, formFromQuote, formToRequest, hasRouteAndCargo } from "@/features/public/quoteQuery";

const EXAMPLES = {
  sea: () => ({
    ...emptyForm(),
    origin: { unlocode: "CNSHA", name: "Shanghai", label: "Shanghai", query: "CNSHA" },
    destination: { unlocode: "FIHEL", name: "Helsinki", label: "Helsinki", query: "FIHEL" },
    mode: "sea",
    cargo: { ...emptyForm().cargo, load_type: "FCL", containers: [{ type: "40HC", count: 1 }] },
  }),
  road: () => ({
    ...emptyForm(),
    origin: { unlocode: "EETLL", name: "Tallinn", label: "Tallinn", query: "EETLL" },
    destination: { unlocode: "FITMP", name: "Tampere", label: "Tampere", query: "FITMP" },
    mode: "road",
    cargo: { ...emptyForm().cargo, load_type: "loose", packages: 2, package_type: "pallet_eur", weight_kg: 600 },
  }),
};

function ResultSkeleton({ label }) {
  return (
    <div aria-busy="true" className="flex flex-col gap-3">
      <span className="sr-only">{label}</span>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Skeleton className="h-5 w-72 max-w-full" />
        <Skeleton className="h-4 w-24" />
      </div>
      <Skeleton className="h-4 w-[28rem] max-w-full" />
      <div className="flex flex-col gap-2 pt-1">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-[4.25rem] w-full rounded-xl" style={{ opacity: 1 - i * 0.2 }} />
        ))}
      </div>
    </div>
  );
}

function EmptyPrompt({ onExample }) {
  const { t } = useT();
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-dashed border-[color:var(--border-strong)] px-5 py-6">
      <div className="flex items-start gap-3">
        <Route className="mt-0.5 size-5 shrink-0 text-ink-tertiary" aria-hidden />
        <div className="flex flex-col gap-1">
          <p className="text-base font-medium">{t("pub.q.empty.title")}</p>
          <p className="max-w-[72ch] text-sm text-ink-secondary">{t("pub.q.empty.body")}</p>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2 pl-8">
        <span className="text-xs text-ink-tertiary">{t("pub.q.examples")}:</span>
        <Button size="sm" variant="outline" onClick={() => onExample("sea")}>{t("pub.q.ex.sea")}</Button>
        <Button size="sm" variant="outline" onClick={() => onExample("road")}>{t("pub.q.ex.road")}</Button>
      </div>
    </div>
  );
}

function NotQuoted({ items }) {
  const { t } = useT();
  if (!items?.length) return null;
  return (
    <details className="group rounded-xl px-3 py-2 shadow-[inset_0_0_0_0.5px_var(--border-default)]">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-sm text-ink-secondary [&::-webkit-details-marker]:hidden">
        <ChevronDown className="size-4 text-ink-tertiary transition-transform group-open:rotate-180" aria-hidden />
        {t("pub.q.not_quoted", { n: items.length })}
      </summary>
      <ul className="mt-2 flex flex-col divide-y divide-[color:var(--border-subtle)] text-sm">
        {items.map((it, i) => (
          <li key={i} className="flex flex-col gap-0.5 py-2 sm:flex-row sm:gap-4">
            <span className="shrink-0 font-medium sm:w-56">
              {it.carrier_name || "—"}
              {it.service_name ? <span className="block text-xs font-normal text-ink-tertiary">{it.service_name}</span> : null}
            </span>
            <span className="text-ink-secondary">{it.reason || "—"}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}

export default function InstantQuote() {
  const { t, locale } = useT();
  const navigate = useNavigate();
  const auth = useAuth();
  const user = auth?.user;
  const [params, setParams] = useSearchParams();
  const [form, setForm] = useState(() => formFromParams(params));
  const [state, setState] = useState({ status: "idle", quote: null, token: null, error: null });
  const [booking, setBooking] = useState({ busyId: null, error: null });
  const ran = useRef(false);

  useEffect(() => {
    const prev = document.title;
    document.title = t("pub.q.doc_title");
    return () => {
      document.title = prev;
    };
  }, [t]);

  const run = useCallback(
    async (f) => {
      setState((s) => ({ ...s, status: "loading", error: null }));
      setBooking({ busyId: null, error: null });
      try {
        const r = await apiPost("/public/quote", formToRequest(f));
        const quote = r?.quote || null;
        const token = r?.claim_token || null;
        setState({ status: "done", quote, token, error: null });
        if (quote) setForm(formFromQuote(quote));
        if (quote?.id && token) setParams({ id: quote.id, token }, { replace: true });
      } catch (e) {
        setState((s) => ({ ...s, status: "error", error: e }));
      }
    },
    [setParams]
  );

  // First load: a saved result (?id=&token=) or inputs handed over from the landing widget.
  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const id = params.get("id");
    const token = params.get("token");
    if (id && token) {
      setState({ status: "loading", quote: null, token, error: null });
      apiGet(`/public/quote/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}`)
        .then((r) => {
          setState({ status: "done", quote: r?.quote || null, token, error: null });
          if (r?.quote) setForm(formFromQuote(r.quote));
        })
        .catch((e) => setState({ status: "load_failed", quote: null, token: null, error: e }));
      return;
    }
    const f = formFromParams(params);
    if (hasRouteAndCargo(f)) run(f);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Refresh the stored quote while procurement is active, fenced to this quote and token.
  const waitingForRates = ['requested', 'partial'].includes(state.quote?.sourcing?.status);
  useEffect(() => {
    if (!waitingForRates || !state.quote?.id || !state.token) return undefined;
    const id = state.quote.id;
    const token = state.token;
    const controller = new AbortController();
    const timer = setInterval(async () => {
      try {
        const r = await apiGet(`/public/quote/${id}?token=${encodeURIComponent(token)}`, { signal: controller.signal });
        setState(s => s.quote?.id === id && s.token === token ? { ...s, quote: r.quote } : s);
      } catch (e) { /* Keep the last price and the explicit refresh action on a transient failure. */ }
    }, 15000);
    return () => { clearInterval(timer); controller.abort(); };
  }, [waitingForRates, state.quote?.id, state.token]);

  const quote = state.quote;
  const options = quote?.options || [];
  const expired = quote && isExpired(quote.valid_until);
  const isCustomer = user?.org_type === "customer";
  const isOtherUser = user && !isCustomer;
  const claimNext = state.token ? `/portal/quotes?claim=${encodeURIComponent(state.token)}` : "/portal/quotes";

  const storeClaim = (option) => {
    try {
      localStorage.setItem("huo.claim", JSON.stringify({ token: state.token, quote_id: quote?.id, option_id: option?.id || null, at: new Date().toISOString() }));
    } catch (e) {}
  };

  const onBook = async (option) => {
    if (!state.token) return;
    if (isCustomer) {
      setBooking({ busyId: option.id, error: null });
      try {
        const r = await apiPost("/quotes/claim", { token: state.token });
        const qid = r?.quote?.id || r?.id || quote?.id;
        navigate(`/portal/quotes/${qid}`);
      } catch (e) {
        setBooking({ busyId: null, error: e });
      }
      return;
    }
    storeClaim(option);
    // Signup claims ?claim= right after account creation; huo.claim + next are the fallback (claim is idempotent).
    navigate(`/signup?claim=${encodeURIComponent(state.token)}&next=${encodeURIComponent(`/portal/quotes?claim=${state.token}`)}`);
  };

  const errorMessage = (e) => {
    if (!e) return null;
    if (e.status === 429) {
      // Backend sentence is English-only; keep its wait time and say it in the page language.
      const m = /(\d+)\s*min/i.exec(e.message || "");
      return { message: m ? t("pub.q.rate_limited_min", { n: m[1] }) : t("pub.q.rate_limited") };
    }
    if (e.status === 0) return e;
    return { message: e.message && !/^Request failed/.test(e.message) ? e.message : t("pub.q.error") };
  };

  const req = quote?.request || {};
  const sellerPaysMain = options.some((o) => (o.not_included || []).some((n) => n.reason_code === "incoterm.seller"));
  const cargoText = pubCargoSummary(
    req.cargo ? { ...req.cargo, load_type: req.cargo.load_type === "FCL" ? "FCL" : "loose" } : null,
    t,
    locale
  );

  return (
    <PublicLayout>
      <div className="mx-auto flex max-w-[1200px] flex-col gap-5 px-4 pb-16 pt-6 sm:px-6 md:pt-8">
        <header className="flex flex-col gap-1">
          <h1 className="text-xl leading-7 md:text-2xl md:leading-9">{t("pub.q.title")}</h1>
          <p className="text-sm text-ink-secondary">{t("pub.q.lead")}</p>
        </header>

        <div className="surface-raised rounded-2xl bg-surface p-3 md:p-4">
          <QuoteForm variant="bar" value={form} onChange={setForm} onSubmit={run} busy={state.status === "loading"} />
        </div>

        <section aria-label={t("pub.q.results")} aria-live="polite" aria-busy={state.status === "loading"} className="flex flex-col gap-3">
          {state.status === "idle" && <EmptyPrompt onExample={(k) => { const f = EXAMPLES[k](); setForm(f); run(f); }} />}

          {state.status === "load_failed" && (
            <>
              <ErrorState error={{ message: t("pub.q.load_failed") }} />
              <EmptyPrompt onExample={(k) => { const f = EXAMPLES[k](); setForm(f); run(f); }} />
            </>
          )}

          {state.status === "error" && state.error?.status === 429 && (
            <div role="alert" className="status-pending flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-3 py-2 text-sm">
              <span className="min-w-0 flex-1">{errorMessage(state.error).message}</span>
              <Link to="/login?next=%2Fportal%2Fquotes%2Fnew" className="font-medium underline underline-offset-4">{t("pub.q.sign_in_quote")}</Link>
            </div>
          )}
          {state.status === "error" && state.error?.status !== 429 && (
            <ErrorState error={errorMessage(state.error)} onRetry={() => run(form)} />
          )}

          {state.status === "loading" && !quote && <ResultSkeleton label={t("pub.q.pricing")} />}

          {(state.status === "done" || state.status === "loading") && quote && (
            <div className={cn("flex flex-col gap-3 transition-opacity", state.status === "loading" && "pointer-events-none opacity-60")}>
              {state.status === "loading" && (
                <p className="flex items-center gap-2 text-xs text-ink-tertiary">
                  <RefreshCw className="size-3.5 animate-spin" aria-hidden /> {t("pub.q.pricing")}
                </p>
              )}
              <div className="flex flex-col gap-1.5">
                <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-1">
                  <div className="flex min-w-0 flex-col gap-0.5">
                    <RouteText origin={req.origin} destination={req.destination} short className="text-base font-medium" />
                    <span className="text-sm text-ink-secondary tabular">
                      {[
                        cargoText,
                        req.ready_date ? t("pub.q.ready", { date: fmtDate(req.ready_date, locale) }) : null,
                        req.incoterm ? `${req.incoterm}${req.incoterm_place ? ` ${req.incoterm_place}` : ""}` : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </div>
                  <span className="flex items-center gap-3 text-sm text-ink-tertiary tabular">
                    {options.length === 1 ? t("pub.q.count_one") : t("pub.q.count", { n: options.length })}
                    {quote.id && state.token && options.length > 0 && (
                      <a
                        href={fileUrl(`/public/quote/${encodeURIComponent(quote.id)}/pdf?token=${encodeURIComponent(state.token)}&lang=${locale}`)}
                        className="inline-flex items-center gap-1 text-ink-secondary hover:text-foreground"
                      >
                        <FileDown className="size-4" aria-hidden /> PDF
                      </a>
                    )}
                  </span>
                </div>
                <p className="flex flex-wrap items-center gap-2 text-sm text-ink-secondary">
                  <span className="inline-flex h-5 items-center rounded-full border border-dashed border-[color:var(--border-strong)] px-1.5 text-[11px]">
                    {t(options.some(o => o.firmness !== "firm") ? "quotes.source.benchmark" : options.some(isIndicative) ? "gq.indicative" : "gq.firm")}
                  </span>
                  <span className="tabular">
                    {t("pub.q.scope", { valid: fmtDate(quote.valid_until, locale), asof: fmtDate(quote.created_at, locale) })}
                  </span>
                </p>
              </div>

              {sellerPaysMain && (
                <p className="flex items-start gap-2 rounded-lg bg-secondary px-3 py-2 text-sm text-ink-secondary">
                  <Info className="mt-0.5 size-4 shrink-0 text-ink-tertiary" aria-hidden />
                  <span>{t("pub.q.seller_pays", { incoterm: req.incoterm || "" })}</span>
                </p>
              )}

              {expired && (
                <div className="status-expired flex flex-wrap items-center gap-3 rounded-lg px-3 py-2 text-sm">
                  <span className="flex-1">{t("pub.q.expired")}</span>
                  <Button size="xs" variant="outline" onClick={() => run(form)}><RefreshCw /> {t("pub.q.reprice")}</Button>
                </div>
              )}

              <SourcingStatus quote={quote} token={state.token} onRefresh={async () => {
                try { const r = await apiGet(`/public/quote/${quote.id}?token=${encodeURIComponent(state.token)}`);
                  setState(s => ({ ...s, quote: r.quote }));
                } catch (e) { setBooking({ busyId: null, error: e }); }
              }} />
              <WarningsList warnings={quote.warnings} />

              {booking.error && <ErrorState error={{ message: t("pub.q.claim_failed", { msg: booking.error.message }) }} />}

              {options.length === 0 ? (
                <div className="flex flex-col gap-2 rounded-xl border border-dashed border-[color:var(--border-strong)] px-5 py-6">
                  <p className="text-base font-medium">{t("pub.q.none.title")}</p>
                  <p className="max-w-[72ch] text-sm text-ink-secondary">{t("pub.q.none.body")}</p>
                  <div className="flex flex-wrap gap-2 pt-1">
                    <Button asChild size="sm" variant="outline"><a href={`mailto:${CONTACT_EMAIL}`}><Mail /> {CONTACT_EMAIL}</a></Button>
                    <Button asChild size="sm" variant="ghost"><Link to="/signup">{t("pub.q.create_account")} <ArrowRight /></Link></Button>
                  </div>
                </div>
              ) : (
                <QuoteOptions
                  options={options}
                  onSelect={expired ? undefined : onBook}
                  selectLabel={t("pub.q.book")}
                  busyId={booking.busyId}
                  disabledReason={isOtherUser ? t("pub.q.staff_reason") : null}
                  incoterm={req.incoterm || null}
                  incotermPlace={req.incoterm_place || null}
                  validUntil={quote.valid_until}
                  pricedAt={quote.created_at}
                  emptyText={t("pub.q.none.title")}
                />
              )}

              <NotQuoted items={quote.not_quoted} />

              {options.length > 0 && !user && (
                <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-ink-secondary">
                  <span>{t("pub.q.book_note")}</span>
                  <Link
                    to={`/login?next=${encodeURIComponent(claimNext)}`}
                    onClick={() => storeClaim(null)}
                    className="text-ink-accent underline-offset-4 hover:underline"
                  >
                    {t("pub.q.have_account")}
                  </Link>
                </p>
              )}
              {isOtherUser && user?.org_type === "internal" && quote?.id && (
                <p className="text-sm">
                  <Link to={`/ops/quotes/${quote.id}`} className="text-ink-accent underline-offset-4 hover:underline">{t("pub.q.open_ops")}</Link>
                </p>
              )}
            </div>
          )}
        </section>
      </div>
    </PublicLayout>
  );
}
