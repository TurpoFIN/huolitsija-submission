import { EnvironmentBanner, OperationalVisibility } from "@/features/OperationalVisibility";
import React, { useEffect, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { Bell, Globe, LogOut, Menu, Moon, PanelLeft, Sun, User } from "lucide-react";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { apiGet, apiPatch, apiPost } from "@/lib/api";
import { relTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Logo } from "@/components/app/Logo";
import { settingsPath } from "@/nav";

export function useTheme() {
  const [theme, setThemeState] = useState(() => document.documentElement.getAttribute("data-theme") || "light");
  const setTheme = (t) => {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("huo.theme", t); } catch (e) {}
    setThemeState(t);
  };
  return [theme, setTheme];
}

function NavItems({ groups, onNavigate }) {
  const { t } = useT();
  return (
    <nav className="flex flex-col gap-4" aria-label={t("review.nav_main")}>
      {groups.map((g, gi) => (
        <div key={gi} className="flex flex-col gap-0.5">
          {g.label && <p className="px-2 pb-1 text-xs text-ink-tertiary">{t(g.label)}</p>}
          {g.items.map((it) => (
            <NavLink key={it.to} to={it.to} end={it.end} onClick={onNavigate}
              className={({ isActive }) => cn("sidebar-pressable flex h-8 items-center gap-2.5 rounded-lg px-2 text-sm text-ink-secondary hover:bg-hover hover:text-foreground",
                isActive && "surface-raised sidebar-nav-active text-foreground font-medium")}>
              {it.icon && <it.icon className="size-4 shrink-0" aria-hidden />}
              <span className="truncate">{t(it.label)}</span>
              {it.badge ? <span className="ml-auto rounded-full bg-secondary px-1.5 text-[11px] tabular text-ink-secondary">{it.badge}</span> : null}
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}

function Notifications() {
  const { t, locale } = useT();
  const navigate = useNavigate();
  const [data, setData] = useState({ items: [], unread: 0 });
  const load = () => apiGet("/notifications").then(setData).catch(() => {});
  useEffect(() => { load(); const id = setInterval(load, 60000); return () => clearInterval(id); }, []);
  return (
    <Popover onOpenChange={(o) => o && load()}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={t("notifications.title")} className="relative">
          <Bell />
          {data.unread > 0 && <span className="absolute right-1 top-1 size-2 rounded-full bg-[var(--danger-500)]" aria-label={`${data.unread}`} />}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0 surface-overlay">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <p className="text-sm font-medium">{t("notifications.title")}</p>
          {data.unread > 0 && <Button size="xs" variant="ghost" onClick={() => apiPost("/notifications/read", {}).then(load)}>{t("notifications.mark_read")}</Button>}
        </div>
        <div className="max-h-96 overflow-y-auto">
          {data.items.length === 0 && <p className="px-3 py-6 text-sm text-ink-tertiary">{t("notifications.empty")}</p>}
          {data.items.map((n) => (
            <button key={n.id} type="button" onClick={() => n.link && navigate(n.link)}
              className={cn("block w-full border-b border-[color:var(--border-subtle)] px-3 py-2 text-left hover:bg-hover", !n.read && "bg-[var(--bg-subtle)]")}>
              <p className="text-sm">{n.title}</p>
              {n.body && <p className="text-xs text-ink-tertiary line-clamp-2">{n.body}</p>}
              <p className="mt-0.5 text-[11px] text-ink-tertiary">{relTime(n.created_at, locale)}</p>
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

function UserMenu() {
  const { user, company, logout, refresh } = useAuth();
  const { t, locale, setLocale } = useT();
  const [theme, setTheme] = useTheme();
  const navigate = useNavigate();
  const changeLocale = async (l) => {
    setLocale(l);
    await apiPatch("/auth/profile", { locale: l }).catch(() => {});
    refresh();
  };
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button type="button" className="flex h-8 items-center gap-2 rounded-full pl-1 pr-2 hover:bg-hover" aria-label={t("nav.profile")}>
          <span className="flex size-6 items-center justify-center rounded-full bg-secondary text-[11px] font-medium">
            {(user?.name || user?.email || "?").split(" ").map((s) => s[0]).slice(0, 2).join("").toUpperCase()}
          </span>
          <span className="hidden max-w-[140px] truncate text-sm sm:inline">{user?.name}</span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60">
        <DropdownMenuLabel className="font-normal">
          <p className="text-sm font-medium truncate">{user?.name}</p>
          <p className="text-xs text-ink-tertiary truncate">{user?.email}</p>
          {company && <p className="text-xs text-ink-tertiary truncate">{company.name}</p>}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => navigate(settingsPath(user?.org_type))}>
          <User className="size-4" /> {t("nav.profile")}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={(e) => { e.preventDefault(); changeLocale(locale === "fi" ? "en" : "fi"); }}>
          <Globe className="size-4" /> {locale === "fi" ? "English" : "Suomi"}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={(e) => { e.preventDefault(); setTheme(theme === "dark" ? "light" : "dark"); }}>
          {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />} {theme === "dark" ? t("theme.light") : t("theme.dark")}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={async () => { await logout(); navigate("/login"); }}>
          <LogOut className="size-4" /> {t("nav.signout")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Authenticated console: 208px sidebar on the stage gradient, inset glass window with a 56px
 * sticky header, 16px workspace padding. Mobile (<768px): 288px sheet.
 * groups: [{ label?: i18nKey, items: [{ to, label: i18nKey, icon, end?, badge? }] }]
 */
export function AppShell({ groups, area, children, headerExtra }) {
  const [open, setOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("huo.sidebar") === "collapsed");
  const loc = useLocation();
  const { t } = useT();
  useEffect(() => setOpen(false), [loc.pathname]);
  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        setCollapsed((c) => { const n = !c; localStorage.setItem("huo.sidebar", n ? "collapsed" : "open"); return n; });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const toggle = () => setCollapsed((c) => { const n = !c; localStorage.setItem("huo.sidebar", n ? "collapsed" : "open"); return n; });

  return (
    <div className="helios-app-stage flex min-h-screen">
      <aside className={cn("hidden md:flex sticky top-0 h-screen w-[208px] shrink-0 flex-col px-2 py-3", collapsed && "md:hidden")} data-slot="sidebar-container">
        <Link to={area === "ops" ? "/ops" : area === "partner" ? "/partner" : "/portal"} className="mb-4 flex items-center gap-2 px-2">
          <Logo />
          <span className="text-xs text-ink-tertiary">{area === "ops" ? t("review.ops") : area === "partner" ? t("enum.role.partner") : ""}</span>
        </Link>
        <div className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
          <NavItems groups={groups} />
        </div>
      </aside>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="left" className="w-[288px] p-3 bg-[var(--sidebar)]">
          <SheetTitle className="mb-4 px-2"><Logo /></SheetTitle>
          <NavItems groups={groups} onNavigate={() => setOpen(false)} />
        </SheetContent>
      </Sheet>
      <div className={cn("helios-app-window flex min-w-0 flex-1 flex-col md:my-2 md:mr-2 md:rounded-2xl", collapsed && "md:ml-2")}>
        <header className="helios-app-navbar sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 px-4 app-header md:rounded-t-2xl">
          <Button variant="ghost" size="icon-sm" className="md:hidden" onClick={() => setOpen(true)} aria-label={t("review.menu")}><Menu /></Button>
          <Button variant="ghost" size="icon-sm" className="hidden md:inline-flex" onClick={toggle} aria-label={t("review.sidebar")}><PanelLeft /></Button>
          <div className="min-w-0 flex-1">{headerExtra}{area === "ops" && <OperationalVisibility />}</div>
          <Notifications />
          <UserMenu />
        </header>
        {area !== "ops" && <EnvironmentBanner />}
        <main className="min-w-0 flex-1 p-4">{children}</main>
      </div>
    </div>
  );
}
