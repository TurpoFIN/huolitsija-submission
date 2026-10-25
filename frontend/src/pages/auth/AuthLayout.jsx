import React from "react";
import { Link } from "react-router-dom";
import { Logo } from "@/components/app/Logo";
import { useT } from "@/lib/i18n";

export function AuthLayout({ title, children, aside, footer }) {
  const { t, locale, setLocale } = useT();
  return (
    <div className="helios-app-stage min-h-screen flex flex-col">
      <header className="flex h-14 items-center justify-between px-4 sm:px-8">
        <Link to="/" aria-label="Huolitsija"><Logo size="lg" /></Link>
        <button type="button" className="text-sm text-ink-secondary hover:text-foreground" onClick={() => setLocale(locale === "fi" ? "en" : "fi")}>
          {locale === "fi" ? "English" : "Suomi"}
        </button>
      </header>
      <main className="flex flex-1 items-start justify-center px-4 pb-12 pt-6 sm:pt-16">
        <div className={aside ? "grid w-full max-w-4xl gap-10 md:grid-cols-[1fr_360px]" : "w-full max-w-sm"}>
          {aside && <div className="hidden md:block pt-2">{aside}</div>}
          <div className="surface-raised rounded-2xl bg-surface p-6">
            <h1 className="mb-5 text-xl">{title}</h1>
            {children}
          </div>
        </div>
      </main>
      <footer className="px-4 pb-6 text-center text-xs text-ink-tertiary">{footer || <Link to="/" className="hover:text-foreground">{t("auth.back_home")}</Link>}</footer>
    </div>
  );
}

export function Field({ label, hint, error, children, htmlFor }) {
  return (
    <div className="mb-3">
      <label htmlFor={htmlFor} className="mb-1 block text-sm font-medium">{label}</label>
      {children}
      {hint && !error && <p className="mt-1 text-xs text-ink-tertiary">{hint}</p>}
      {error && <p className="mt-1 text-xs text-danger-fg" role="alert">{error}</p>}
    </div>
  );
}
