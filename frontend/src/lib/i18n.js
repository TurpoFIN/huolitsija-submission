// Tiny i18n: Finnish + English. Dictionaries live in src/i18n/<area>.js as { en: {...}, fi: {...} }
// with flat dotted keys ("shipments.title"). Missing Finnish falls back to English, then the key.
import React, { createContext, useCallback, useContext, useMemo, useState, useEffect } from "react";
import dictionaries from "@/i18n";

const I18nCtx = createContext(null);

function initialLocale() {
  try {
    const saved = localStorage.getItem("huo.locale");
    if (saved === "fi" || saved === "en") return saved;
  } catch (e) {}
  return "fi";
}

export function I18nProvider({ children }) {
  const [locale, setLocaleState] = useState(initialLocale);
  useEffect(() => { document.documentElement.setAttribute("lang", locale); }, [locale]);
  const setLocale = useCallback((l) => {
    setLocaleState(l);
    try {
      localStorage.setItem("huo.locale", l);
    } catch (e) {}
    document.documentElement.setAttribute("lang", l);
  }, []);
  const t = useCallback((key, vars) => translate(dictionaries, locale, key, vars), [locale]);
  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);
  return <I18nCtx.Provider value={value}>{children}</I18nCtx.Provider>;
}

/** One text: the locale's string, else English, else the key. With vars.n === 1 the "<key>_one" form is used when the
 *  locale has it (Finnish counts: "1 virhe", "3 virhettä"). {name} placeholders are filled from vars. */
export function translate(dicts, locale, key, vars) {
  const one = vars && Number(vars.n) === 1 ? dicts[locale]?.[`${key}_one`] ?? (dicts[locale]?.[key] === undefined ? dicts.en?.[`${key}_one`] : undefined) : undefined;
  let s = one ?? dicts[locale]?.[key] ?? dicts.en?.[key] ?? key;
  if (vars) Object.entries(vars).forEach(([k, v]) => (s = s.split(`{${k}}`).join(String(v))));
  return s;
}

export function useT() {
  const ctx = useContext(I18nCtx);
  if (!ctx) throw new Error("useT outside I18nProvider");
  return ctx;
}

/** Label for an enum value: tEnum(t, "status", "in_transit") -> t("enum.status.in_transit") or a humanised fallback. */
export function tEnum(t, group, value) {
  if (value === null || value === undefined || value === "") return "—";
  const key = `enum.${group}.${value}`;
  const s = t(key);
  return s === key ? String(value).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : s;
}

/** A subtree in its own language (e.g. the demo flow map defaults to English) without changing the user's app locale. */
export function LocaleScope({ locale, setLocale, children }) {
  const t = useCallback((key, vars) => translate(dictionaries, locale, key, vars), [locale]);
  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);
  return <I18nCtx.Provider value={value}>{children}</I18nCtx.Provider>;
}
