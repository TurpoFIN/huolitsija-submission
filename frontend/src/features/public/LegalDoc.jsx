/**
 * Long-form legal page: title, version and effective date, table of contents, numbered sections at ~72ch.
 *
 * <LegalDoc title lead version date sections={[{id, title, blocks: [Block]}]}>{optional form after the sections}</LegalDoc>
 * Block: {p: "text with [links](https://…)"} | {ul: ["…"]} | {table: {head: ["…"], rows: [["…"]]}} | {note: "…"}
 */
import React from "react";
import { ChevronDown } from "lucide-react";
import { useT } from "@/lib/i18n";
import { date as fmtDate } from "@/lib/format";
import { PublicLayout } from "./PublicLayout";

const LINK_RE = /\[([^\]]+)\]\(([^)\s]+)\)/g;

function Rich({ text }) {
  const out = [];
  let last = 0;
  let m;
  LINK_RE.lastIndex = 0;
  while ((m = LINK_RE.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const href = m[2];
    const ext = /^https?:/.test(href);
    out.push(
      <a key={m.index} href={href} className="text-ink-accent underline underline-offset-4" {...(ext ? { target: "_blank", rel: "noreferrer" } : {})}>
        {m[1]}
      </a>
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return <>{out}</>;
}

function Block({ b }) {
  if (b.p) return <p className="leading-7"><Rich text={b.p} /></p>;
  if (b.note) return <p className="rounded-lg bg-secondary px-3 py-2 text-sm leading-6 text-ink-secondary"><Rich text={b.note} /></p>;
  if (b.ul)
    return (
      <ul className="flex list-disc flex-col gap-1.5 pl-5 leading-7 marker:text-ink-tertiary">
        {b.ul.map((li, i) => (
          <li key={i}><Rich text={li} /></li>
        ))}
      </ul>
    );
  if (b.table)
    return (
      <div className="overflow-x-auto">
        <table className="w-full table-fixed border-collapse text-sm">
          <thead>
            <tr className="border-b border-[color:var(--border-default)] text-left text-xs text-ink-tertiary">
              {b.table.head.map((h, i) => (
                <th key={i} scope="col" className="break-words py-2 pr-4 font-medium [overflow-wrap:anywhere]">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {b.table.rows.map((r, i) => (
              <tr key={i} className="border-b border-[color:var(--border-subtle)] align-top">
                {r.map((c, j) =>
                  j === 0 ? (
                    <th key={j} scope="row" className="break-words py-2 pr-4 text-left font-normal leading-6 [overflow-wrap:anywhere]">{c}</th>
                  ) : (
                    <td key={j} className="break-words py-2 pr-4 leading-6 text-ink-secondary [overflow-wrap:anywhere]"><Rich text={c} /></td>
                  )
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  return null;
}

function Toc({ sections, label }) {
  return (
    <nav aria-label={label}>
      <ol className="flex flex-col gap-0.5 text-sm">
        {sections.map((s, i) => (
          <li key={s.id}>
            <a href={`#${s.id}`} className="flex gap-2 rounded-md px-2 py-1 text-ink-secondary hover:bg-hover hover:text-foreground">
              <span className="w-5 shrink-0 tabular text-ink-tertiary">{i + 1}.</span>
              <span>{s.title}</span>
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}

export default function LegalDoc({ title, lead, version, date, sections, children }) {
  const { t, locale } = useT();
  return (
    <PublicLayout>
      <div className="mx-auto max-w-[1200px] px-4 pb-16 pt-8 sm:px-6 md:pt-12">
        <div className="grid gap-8 lg:grid-cols-[240px_minmax(0,1fr)] lg:gap-12">
          <aside className="hidden lg:block">
            <div className="sticky top-20">
              <p className="mb-2 px-2 text-xs font-medium text-ink-tertiary">{t("pub.legal.toc")}</p>
              <Toc sections={sections} label={t("pub.legal.toc")} />
            </div>
          </aside>
          <article className="min-w-0 max-w-[72ch]">
            <header className="mb-8 flex flex-col gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-ink-tertiary tabular">{t("domain.legal_version", { v: version, date: fmtDate(date, locale) })}</span>
              </div>
              <h1 className="text-2xl leading-9 tracking-tight md:text-3xl md:leading-[2.625rem]">{title}</h1>
              <p className="text-base text-ink-secondary">{lead}</p>
            </header>
            <details className="mb-8 rounded-xl border border-[color:var(--border-subtle)] px-3 py-2 lg:hidden">
              <summary className="flex cursor-pointer list-none items-center justify-between text-sm font-medium [&::-webkit-details-marker]:hidden">
                {t("pub.legal.toc")}
                <ChevronDown className="size-4 text-ink-tertiary" aria-hidden />
              </summary>
              <div className="pt-2">
                <Toc sections={sections} label={t("pub.legal.toc")} />
              </div>
            </details>
            <div className="flex flex-col gap-10 text-[0.9375rem]">
              {sections.map((s, i) => (
                <section key={s.id} aria-labelledby={s.id}>
                  <h2 id={s.id} className="mb-3 scroll-mt-20 text-lg font-medium leading-7">
                    <span className="mr-2 tabular text-ink-tertiary">{i + 1}.</span>
                    {s.title}
                  </h2>
                  <div className="flex flex-col gap-3 text-foreground">
                    {s.blocks.map((b, j) => (
                      <Block key={j} b={b} />
                    ))}
                  </div>
                </section>
              ))}
            </div>
            {children}
            <p className="mt-12 text-sm">
              <a href="#main" className="text-ink-secondary hover:text-foreground">{t("pub.legal.back_top")} ↑</a>
            </p>
          </article>
        </div>
      </div>
    </PublicLayout>
  );
}
