import React from "react";
import { useT } from "@/lib/i18n";
import { Reasons, bi } from "./extCommon";

export default function OriginPreference({ decl }) {
  const { t, locale } = useT();
  return <ul className="divide-y divide-border">{(decl.items || []).map(it => {
    const a = it.origin_assessment;
    return <li key={it.seq} className="py-3 text-sm">
      <p className="mb-2"><span className="font-mono">#{it.seq} · {it.origin_country || t("cx.missing")}</span> · {it.description}</p>
      {!a ? <p className="text-ink-secondary">{t("cx.origin.not_checked")}</p> : <>
        <p className="mb-2">{t("cx.duty.preference")}: <span className="font-mono">{a.preference_code || t("cx.missing")}</span>{a.arrangement && <> · {bi(locale, a.arrangement, "name") || a.arrangement.code || (typeof a.arrangement === "string" ? a.arrangement : "")}</>}</p>
        <Reasons reasons={a.reasons} />
        {a.proof && <p className="mt-2 text-xs"><span className="text-ink-secondary">{t("cx.origin.proof")}: </span>{[a.proof.type, a.proof.document_code, a.proof.ref].filter(Boolean).join(" · ") || t("cx.missing")} · {t(a.proof.valid === true ? "cx.origin.valid" : "cx.origin.invalid")}</p>}
        {a.psr && <details className="mt-2 text-xs"><summary className="cursor-pointer">{t("cx.origin.rule")}</summary><p className="mt-1 whitespace-pre-wrap">{bi(locale, a.psr.rule_text) || (typeof a.psr.rule === "string" ? a.psr.rule : "") || t("cx.missing")}</p></details>}
      </>}
    </li>;
  })}</ul>;
}
