import React, { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Segmented } from "@/components/app/FilterBar";
import { Section } from "@/components/app/layout";
import { ErrorState } from "@/components/app/states";
import { useTheme } from "@/components/app/AppShell";
import { apiPatch, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { toast } from "sonner";
import { SmsPreference } from "@/features/channels/SmsPreference";

function Row({ label, hint, children }) {
  return (
    <div className="flex flex-col gap-2 border-b border-[color:var(--border-subtle)] px-0 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="min-w-0"><p className="text-sm font-medium">{label}</p>{hint && <p className="text-xs text-ink-tertiary">{hint}</p>}</div>
      <div className="sm:w-72 shrink-0">{children}</div>
    </div>
  );
}

/** Profile, language, theme, notifications and password — label left, control right. */
export function ProfileSettings({ showNotifications = true }) {
  const { t, locale, setLocale } = useT();
  const { user, refresh } = useAuth();
  const [theme, setTheme] = useTheme();
  const [name, setName] = useState(user?.name || "");
  const [phone, setPhone] = useState(user?.phone || "");
  const [pw, setPw] = useState({ current_password: "", new_password: "" });
  const [pwErr, setPwErr] = useState(null);
  const save = async (body) => {
    try { await apiPatch("/auth/profile", body); await refresh(); toast.success(t("action.saved")); } catch (e) { toast.error(e.message); }
  };
  const changePw = async () => {
    setPwErr(null);
    try { await apiPost("/auth/change-password", pw); setPw({ current_password: "", new_password: "" }); toast.success(t("action.saved")); } catch (e) { setPwErr(e); }
  };
  return (
    <>
      <Section title={t("settings.profile")}>
        <Row label={t("field.name")}><div className="flex gap-2"><Input value={name} onChange={(e) => setName(e.target.value)} /><Button variant="outline" disabled={!name || name === user?.name} onClick={() => save({ name })}>{t("action.save")}</Button></div></Row>
        <Row label={t("field.email")}><Input value={user?.email || ""} disabled /></Row>
        <Row label={t("settings.phone")}><div className="flex gap-2"><Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+358…" /><Button variant="outline" disabled={phone === (user?.phone || "")} onClick={() => save({ phone })}>{t("action.save")}</Button></div></Row>
      </Section>
      <Section title={t("settings.appearance")}>
        <Row label={t("field.language")}>
          <Segmented value={locale} label={t("field.language")} onChange={(l) => { setLocale(l); save({ locale: l }); }}
            options={[{ value: "fi", label: "Suomi" }, { value: "en", label: "English" }]} />
        </Row>
        <Row label={t("field.theme")}>
          <Segmented value={theme} label={t("field.theme")} onChange={setTheme} options={[{ value: "light", label: t("theme.light") }, { value: "dark", label: t("theme.dark") }]} />
        </Row>
        {showNotifications && (
          <Row label={t("settings.notifications")} hint={t("settings.notifications_hint")}>
            <Switch checked={user?.notify_email !== false} onCheckedChange={(v) => save({ notify_email: v })} aria-label={t("settings.notifications")} />
          </Row>
        )}
        {showNotifications && <SmsPreference Row={Row} phone={user?.phone} />}
      </Section>
      <Section title={t("settings.password")}>
        <Row label={t("settings.current_password")}><Input type="password" autoComplete="current-password" value={pw.current_password} onChange={(e) => setPw({ ...pw, current_password: e.target.value })} /></Row>
        <Row label={t("settings.new_password")} hint={t("auth.signup.password_hint")}><Input type="password" autoComplete="new-password" value={pw.new_password} onChange={(e) => setPw({ ...pw, new_password: e.target.value })} /></Row>
        {pwErr && <ErrorState className="mt-2" compact error={pwErr} />}
        <div className="mt-3"><Button variant="outline" disabled={!pw.current_password || pw.new_password.length < 10} onClick={changePw}>{t("action.save")}</Button></div>
      </Section>
    </>
  );
}
