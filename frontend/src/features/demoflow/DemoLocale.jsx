import React, { useCallback, useState } from "react";
import { LocaleScope, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

// The demo runs in one language end to end: English by default (the agent's e-mails and trace are English), FI on request.
// Its own choice, so the user's app locale stays as it is.
const KEY = "huo.demoflow.locale";

export function DemoLocale({ children }) {
  const [locale, set] = useState(() => (localStorage.getItem(KEY) === "fi" ? "fi" : "en"));
  const setLocale = useCallback((l) => { localStorage.setItem(KEY, l); set(l); }, []);
  return <LocaleScope locale={locale} setLocale={setLocale}>{children}</LocaleScope>;
}

export function LangToggle() {
  const { locale, setLocale, t } = useT();
  return (
    <div className="df-lang" role="radiogroup" aria-label={t("demoflow.lang")}>
      {["en", "fi"].map((l) => (
        <button key={l} type="button" role="radio" aria-checked={locale === l} className={cn("df-seg-btn", locale === l && "df-seg-on")} onClick={() => setLocale(l)}>
          {l.toUpperCase()}
        </button>
      ))}
    </div>
  );
}
