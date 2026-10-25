// Public homepage (V2): headline + instant-quote widget above the fold, proof points, services, how it works,
// pricing anatomy, FAQ. No invented stats, logos or testimonials.
import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowRight, FileCheck2, Landmark, Mail, MapPinned, Plane, Receipt, ShieldCheck, Ship, TrainFront, Truck,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useT } from "@/lib/i18n";
import { PublicLayout, CONTACT_EMAIL } from "@/features/public/PublicLayout";
import QuoteForm from "@/features/public/QuoteForm";
import Faq from "@/features/public/Faq";
import { emptyForm, formToParams } from "@/features/public/quoteQuery";

const PROOF = [
  ["customs", Landmark],
  ["track", MapPinned],
  ["docs", FileCheck2],
  ["price", Receipt],
];
const SERVICES = [
  ["sea", Ship],
  ["road", Truck],
  ["air", Plane],
  ["rail", TrainFront],
  ["customs", Landmark],
  ["extra", ShieldCheck],
];
const STEPS = ["quote", "book", "docs", "track", "pay"];
const PRICE_PARTS = ["origin", "main", "dest", "customs", "pass"];
const FAQ = ["incoterms", "nsab", "rep", "vat", "price", "consumer"];

function SectionHead({ id, title, lead }) {
  return (
    <div className="mb-6 flex max-w-2xl flex-col gap-1.5">
      <h2 id={id} className="scroll-mt-20 text-xl leading-7 md:text-2xl md:leading-9">{title}</h2>
      {lead && <p className="text-sm text-ink-secondary md:text-base">{lead}</p>}
    </div>
  );
}

export default function Landing() {
  const { t } = useT();
  const navigate = useNavigate();
  const [form, setForm] = useState(emptyForm);

  return (
    <PublicLayout>
      {/* Hero: decision first — where from, where to, what. */}
      <section aria-labelledby="hero-title" className="mx-auto max-w-[1200px] px-4 pb-10 pt-6 sm:px-6 md:pt-8">
        <div className="grid items-center gap-6 md:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] md:gap-10">
          <div className="flex flex-col gap-3">
            <p className="text-xs font-medium text-ink-tertiary md:text-sm">{t("pub.hero.eyebrow")}</p>
            <h1 id="hero-title" className="text-[1.75rem] leading-[2.125rem] tracking-tight md:text-[2.5rem] md:leading-[2.875rem]">
              {t("pub.hero.title")}
            </h1>
            <p className="max-w-[60ch] text-sm leading-6 text-ink-secondary md:text-md">{t("pub.hero.sub")}</p>
          </div>
          <div className="hidden md:block" aria-hidden>
            <img src="/brand/hero.webp" alt="" width="1200" height="800" className="block aspect-[2/1] w-full object-cover object-[50%_38%] mix-blend-multiply dark:hidden" />
            <img src="/brand/hero-dark.webp" alt="" width="1200" height="800" className="hidden aspect-[2/1] w-full object-cover object-[50%_38%] mix-blend-lighten [filter:brightness(0.92)_contrast(1.12)] dark:block [mask-image:radial-gradient(ellipse_72%_70%_at_50%_50%,black_62%,transparent_100%)]" />
          </div>
        </div>
        <div className="surface-raised mt-6 rounded-2xl bg-surface p-3 md:mt-6 md:p-4">
          <QuoteForm
            variant="hero"
            value={form}
            onChange={setForm}
            onSubmit={(f) => navigate(`/quote?${formToParams(f).toString()}`)}
          />
          <p className="mt-2 text-xs text-ink-tertiary">{t("pub.w.hint")}</p>
        </div>
        <div className="mt-6 md:hidden">
          <img src="/brand/hero.webp" alt={t("pub.hero.img_alt")} width="1200" height="800" className="block aspect-[2/1] max-h-[30vh] w-full object-cover object-[50%_38%] mix-blend-multiply dark:hidden" />
          <img src="/brand/hero-dark.webp" alt={t("pub.hero.img_alt")} width="1200" height="800" className="hidden aspect-[2/1] max-h-[30vh] w-full object-cover object-[50%_38%] mix-blend-lighten [filter:brightness(0.92)_contrast(1.12)] dark:block [mask-image:radial-gradient(ellipse_72%_70%_at_50%_50%,black_62%,transparent_100%)]" />
        </div>
      </section>

      {/* Proof points */}
      <section aria-labelledby="proof-title" className="mx-auto max-w-[1200px] px-4 pb-14 sm:px-6">
        <h2 id="proof-title" className="sr-only">{t("pub.proof.heading")}</h2>
        <ul className="grid gap-x-8 gap-y-5 sm:grid-cols-2 lg:grid-cols-4">
          {PROOF.map(([k, Icon]) => (
            <li key={k} className="flex flex-col gap-1.5 border-t border-[color:var(--border-default)] pt-4">
              <span className="flex items-center gap-2 text-sm font-medium">
                <Icon className="size-4 shrink-0 text-ink-tertiary" aria-hidden />
                {t(`pub.proof.${k}.t`)}
              </span>
              <p className="text-sm leading-6 text-ink-secondary">{t(`pub.proof.${k}.b`)}</p>
            </li>
          ))}
        </ul>
      </section>

      {/* Services */}
      <section aria-labelledby="services" className="scroll-mt-16 border-t border-[color:var(--border-subtle)] bg-surface/60">
        <div className="mx-auto max-w-[1200px] px-4 py-12 sm:px-6 md:py-16">
          <SectionHead id="services" title={t("pub.services.title")} lead={t("pub.services.lead")} />
          <ul className="grid gap-x-8 gap-y-6 sm:grid-cols-2 lg:grid-cols-3">
            {SERVICES.map(([k, Icon]) => (
              <li key={k} className="flex gap-3 border-t border-[color:var(--border-subtle)] pt-4">
                <Icon className="mt-0.5 size-5 shrink-0 text-ink-tertiary" aria-hidden />
                <div className="flex min-w-0 flex-col gap-1">
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <h3 className="text-base font-medium">{t(`pub.svc.${k}.t`)}</h3>
                    <span className="text-xs text-ink-tertiary">{t(`pub.svc.${k}.m`)}</span>
                  </div>
                  <p className="text-sm leading-6 text-ink-secondary">{t(`pub.svc.${k}.b`)}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* How it works */}
      <section aria-labelledby="how" className="scroll-mt-16 border-t border-[color:var(--border-subtle)]">
        <div className="mx-auto max-w-[1200px] px-4 py-12 sm:px-6 md:py-16">
          <SectionHead id="how" title={t("pub.how.title")} lead={t("pub.how.lead")} />
          <ol className="grid gap-x-6 gap-y-5 md:grid-cols-5">
            {STEPS.map((k, i) => (
              <li key={k} className="flex gap-3 md:flex-col md:gap-2">
                <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-secondary text-sm font-medium tabular text-ink-secondary">
                  {i + 1}
                </span>
                <div className="flex flex-col gap-1">
                  <h3 className="text-base font-medium">{t(`pub.how.${k}.t`)}</h3>
                  <p className="text-sm leading-6 text-ink-secondary">{t(`pub.how.${k}.b`)}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      {/* Pricing anatomy */}
      <section aria-labelledby="pricing" className="scroll-mt-16 border-t border-[color:var(--border-subtle)] bg-surface/60">
        <div className="mx-auto grid max-w-[1200px] gap-8 px-4 py-12 sm:px-6 md:py-16 lg:grid-cols-[minmax(0,4fr)_minmax(0,7fr)]">
          <div className="flex flex-col gap-4">
            <SectionHead id="pricing" title={t("pub.pricing.title")} lead={t("pub.pricing.lead")} />
            <p className="-mt-2 text-sm leading-6 text-ink-secondary">{t("pub.pricing.note_binding")}</p>
            <p className="text-sm leading-6 text-ink-secondary">{t("pub.pricing.note_vat")}</p>
            <div>
              <Button asChild variant="accent">
                <Link to="/quote">{t("pub.pricing.cta")} <ArrowRight /></Link>
              </Button>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] border-collapse text-sm">
              <caption className="sr-only">{t("pub.pricing.title")}</caption>
              <thead>
                <tr className="border-b border-[color:var(--border-default)] text-left text-xs text-ink-tertiary">
                  <th scope="col" className="py-2 pr-4 font-medium">{t("pub.pricing.col.stage")}</th>
                  <th scope="col" className="py-2 pr-4 font-medium">{t("pub.pricing.col.what")}</th>
                  <th scope="col" className="py-2 font-medium">{t("pub.pricing.col.vat")}</th>
                </tr>
              </thead>
              <tbody>
                {PRICE_PARTS.map((k) => (
                  <tr key={k} className="border-b border-[color:var(--border-subtle)] align-top">
                    <th scope="row" className="py-3 pr-4 text-left font-medium">{t(`pub.pricing.${k}.t`)}</th>
                    <td className="py-3 pr-4 leading-6 text-ink-secondary">{t(`pub.pricing.${k}.b`)}</td>
                    <td className="whitespace-nowrap py-3 text-ink-secondary">{t(`pub.pricing.${k}.v`)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* FAQ */}
      <section aria-labelledby="faq" className="scroll-mt-16 border-t border-[color:var(--border-subtle)]">
        <div className="mx-auto grid max-w-[1200px] gap-6 px-4 py-12 sm:px-6 md:py-16 lg:grid-cols-[minmax(0,4fr)_minmax(0,7fr)] lg:gap-8">
          <SectionHead id="faq" title={t("pub.faq.title")} />
          <Faq items={FAQ.map((k) => ({ id: k, q: t(`pub.faq.${k}.q`), a: t(`pub.faq.${k}.a`) }))} />
        </div>
      </section>

      {/* Closing row */}
      <section aria-labelledby="cta-title" className="border-t border-[color:var(--border-subtle)] bg-surface/60">
        <div className="mx-auto flex max-w-[1200px] flex-col gap-4 px-4 py-10 sm:px-6 md:flex-row md:items-center md:justify-between">
          <div className="flex max-w-2xl flex-col gap-1">
            <h2 id="cta-title" className="text-xl leading-7">{t("pub.cta.title")}</h2>
            <p className="text-sm text-ink-secondary">{t("pub.cta.body")}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="accent" size="lg">
              <Link to="/quote">{t("pub.nav.get_quote")}</Link>
            </Button>
            <Button asChild variant="outline" size="lg">
              <a href={`mailto:${CONTACT_EMAIL}`}><Mail /> {t("pub.cta.email")}</a>
            </Button>
          </div>
        </div>
      </section>
    </PublicLayout>
  );
}
