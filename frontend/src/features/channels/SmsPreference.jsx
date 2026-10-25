import React, { useEffect, useState } from "react";
import { Switch } from "@/components/ui/switch";
import { apiGet, apiPatch } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { toast } from "sonner";

/** Text-message milestones for the signed-in user (s14). Shown only when ops have turned SMS on. */
export function SmsPreference({ Row, phone }) {
  const { t } = useT();
  const [prefs, setPrefs] = useState(null);
  useEffect(() => { apiGet("/channels/me").then(setPrefs).catch(() => setPrefs(null)); }, [phone]);
  if (!prefs?.sms_available) return null;
  const toggle = async (v) => {
    try { setPrefs(await apiPatch("/channels/me", { notify_sms: v, ...(phone ? { phone } : {}) }).then((p) => ({ ...prefs, ...p }))); toast.success(t("action.saved")); }
    catch (e) { toast.error(e.message); }
  };
  return (
    <Row label={t("channels.sms")} hint={prefs.phone ? t("channels.sms_hint", { phone: prefs.phone }) : t("channels.sms_needs_phone")}>
      <Switch checked={!!prefs.notify_sms} disabled={!prefs.phone && !phone} onCheckedChange={toggle} aria-label={t("channels.sms")} />
    </Row>
  );
}
