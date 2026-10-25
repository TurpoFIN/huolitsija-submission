/**
 * Slim public layout (outside AppShell): sticky header, main, footer.
 *
 * <PublicLayout>{page}</PublicLayout>
 * Header: logo, anchors (Services, How it works, Pricing → "/#…"), language switch (FI/EN),
 * theme toggle (data-theme on <html> + localStorage "huo.theme"), Sign in / Open portal, "Get a quote".
 * On < md the anchors, language and theme move into a sheet behind a Menu button.
 */
import React, { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { Menu, Moon, Sun } from "lucide-react";
import { Logo } from "@/components/app/Logo";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useT } from "@/lib/i18n";
import { homeFor, useAuth } from "@/lib/auth";
import { CompanyIdentity, LegalConfigProvider } from "./CompanyIdentity";
import { cn } from "@/lib/utils";

export const CONTACT_EMAIL = "ops@huolitsija.fi";

export function usePublicTheme() {
  const [theme, setThemeState] = useState(() => document.documentElement.getAttribute("data-theme") || "light");
  const setTheme = (t) => {
    document.documentElement.setAttribute("data-theme", t);
    try {
      localStorage.setItem("huo.theme", t);
    } catch (e) {}
    setThemeState(t);
  };
  return [theme, setTheme];
}

export function ThemeToggle({ className }) {
  const { t } = useT();
  const [theme, setTheme] = usePublicTheme();
  const dark = theme === "dark";
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      className={className}
      aria-label={dark ? t("pub.theme.to_light") : t("pub.theme.to_dark")}
      title={dark ? t("pub.theme.to_light") : t("pub.theme.to_dark")}
      onClick={() => setTheme(dark ? "light" : "dark")}
    >
      {dark ? <Sun /> : <Moon />}
    </Button>
  );
}

/** FI | EN segmented switch. */
export function LangSwitch({ className }) {
  const { t, locale, setLocale } = useT();
  return (
    <div role="group" aria-label={t("pub.lang.label")} className={cn("inline-flex h-7 items-center rounded-full bg-secondary p-0.5", className)}>
      {[
        ["fi", "FI", "Suomi"],
        ["en", "EN", "English"],
      ].map(([code, short, name]) => (
        <button
          key={code}
          type="button"
          lang={code}
          aria-label={name}
          aria-pressed={locale === code}
          onClick={() => setLocale(code)}
          className={cn(
            "h-6 rounded-full px-2 text-xs font-medium text-ink-secondary transition-colors hover:text-foreground",
            locale === code && "surface-raised bg-surface text-foreground"
          )}
        >
          {short}
        </button>
      ))}
    </div>
  );
}

const ANCHORS = [
  ["services", "pub.nav.services"],
  ["how", "pub.nav.how"],
  ["pricing", "pub.nav.pricing"],
];

function AuthAction({ className, onNavigate }) {
  const { t } = useT();
  const auth = useAuth();
  const user = auth?.user;
  if (user) {
    return (
      <Button asChild variant="ghost" className={className}>
        <Link to={homeFor(user)} onClick={onNavigate}>{t("pub.nav.portal")}</Link>
      </Button>
    );
  }
  return (
    <Button asChild variant="ghost" className={className}>
      <Link to="/login" onClick={onNavigate}>{t("pub.nav.sign_in")}</Link>
    </Button>
  );
}

export function PublicHeader() {
  const { t } = useT();
  const [open, setOpen] = useState(false);
  return (
    <header className="app-header sticky top-0 z-40 border-b border-[color:var(--border-subtle)]">
      <div className="mx-auto flex h-14 max-w-[1200px] items-center gap-3 px-4 sm:px-6">
        <Link to="/" className="mr-2 shrink-0 rounded-md" aria-label={t("review.home")}>
          <Logo size="lg" />
        </Link>
        <nav aria-label={t("pub.nav.main")} className="hidden items-center gap-0.5 md:flex">
          {ANCHORS.map(([id, key]) => (
            <Button key={id} asChild variant="ghost" className="text-ink-secondary hover:text-foreground">
              <Link to={`/#${id}`}>{t(key)}</Link>
            </Button>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-1.5">
          <LangSwitch className="hidden md:inline-flex" />
          <ThemeToggle className="hidden md:inline-flex" />
          <AuthAction className="hidden sm:inline-flex" />
          <Button asChild variant="accent">
            <Link to="/quote">{t("pub.nav.get_quote")}</Link>
          </Button>
          <Button variant="ghost" size="icon-sm" className="md:hidden" aria-label={t("pub.nav.menu")} onClick={() => setOpen(true)}>
            <Menu />
          </Button>
        </div>
      </div>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="right" className="surface-overlay flex w-72 flex-col gap-1 p-4">
          <SheetTitle className="mb-2 text-sm font-medium">{t("pub.nav.menu")}</SheetTitle>
          {ANCHORS.map(([id, key]) => (
            <Link key={id} to={`/#${id}`} onClick={() => setOpen(false)} className="flex h-9 items-center rounded-lg px-2 text-sm hover:bg-hover">
              {t(key)}
            </Link>
          ))}
          <Link to="/#faq" onClick={() => setOpen(false)} className="flex h-9 items-center rounded-lg px-2 text-sm hover:bg-hover">
            {t("pub.nav.faq")}
          </Link>
          <AuthAction className="justify-start px-2" onNavigate={() => setOpen(false)} />
          <div className="mt-3 flex items-center justify-between border-t border-[color:var(--border-subtle)] pt-3">
            <LangSwitch />
            <ThemeToggle />
          </div>
        </SheetContent>
      </Sheet>
    </header>
  );
}

export function PublicFooter() {
  const { t, locale } = useT();
  return (
    <footer className="border-t border-[color:var(--border-subtle)]">
      <div className="mx-auto flex max-w-[1200px] flex-col gap-6 px-4 py-8 text-sm sm:px-6 md:flex-row md:items-start md:justify-between">
        <div className="flex flex-col gap-2">
          <Logo />
          <p className="text-ink-tertiary">{t("pub.footer.tagline")}</p>
          <CompanyIdentity compact />
        </div>
        <div className="flex flex-col gap-3 md:items-end">
          <nav aria-label={t("review.legal")} className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <Link to="/terms" className="text-ink-secondary hover:text-foreground">{t("pub.footer.terms")}</Link>
            <Link to="/privacy" className="text-ink-secondary hover:text-foreground">{t("pub.footer.privacy")}</Link>
            <Link to="/dpa" className="text-ink-secondary hover:text-foreground">{locale === "fi" ? "Käsittelysopimus" : "DPA"}</Link>
            <Link to="/cookies" className="text-ink-secondary hover:text-foreground">{locale === "fi" ? "Evästeet" : "Cookies"}</Link>
            <Link to="/company" className="text-ink-secondary hover:text-foreground">{locale === "fi" ? "Yritystiedot" : "Company details"}</Link>
            <Link to="/quote" className="text-ink-secondary hover:text-foreground">{t("pub.nav.get_quote")}</Link>
          </nav>
          <p className="text-xs text-ink-tertiary">{t("pub.footer.nsab")}</p>
          <div className="flex items-center gap-2">
            <LangSwitch />
            <ThemeToggle />
          </div>
        </div>
      </div>
    </footer>
  );
}

/** Scrolls to location.hash after route changes (sticky header offset is handled by scroll-mt on targets). */
function useHashScroll() {
  const loc = useLocation();
  useEffect(() => {
    if (!loc.hash) {
      window.scrollTo(0, 0);
      return;
    }
    const id = decodeURIComponent(loc.hash.slice(1));
    const el = document.getElementById(id);
    if (el) el.scrollIntoView({ block: "start" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loc.pathname, loc.hash]);
}

export function PublicLayout({ children }) {
  const { t, locale } = useT();
  useHashScroll();
  useEffect(() => {
    document.documentElement.setAttribute("lang", locale);
  }, [locale]);
  return (
    <LegalConfigProvider><div className="flex min-h-screen flex-col bg-background text-foreground">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-full focus:bg-surface focus:px-3 focus:py-1.5 focus:text-sm">
        {t("pub.skip")}
      </a>
      <PublicHeader />
      <main id="main" className="flex-1">
        {children}
      </main>
      <PublicFooter />
    </div></LegalConfigProvider>
  );
}

export default PublicLayout;
