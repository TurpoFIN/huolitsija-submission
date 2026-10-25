// Filing channel state, recorded Tulli responses, and the declaration message (XML) preview.
import React, { useState } from "react";
import { ChevronDown, ChevronRight, Download, FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/app/states";
import { fileUrl } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";

export function Submission({ decl }) {
  const { t, locale } = useT();
  const sub = decl?.submission;
  const gw = decl?.gateway || {};
  const tr = (k, fallback) => { const s = t(k); return s === k ? fallback : s; };
  if (!sub) {
    return (
      <div className="flex flex-col gap-1 text-sm">
        <p className="text-ink-secondary">{t("customs.sub.none")}</p>
        <p className="text-xs text-ink-tertiary">{tr(`customs.sub.channel.${gw.channel}`, gw.description)} · {t("customs.sub.template", { v: gw.template_version || "—" })}</p>
      </div>
    );
  }
  const msgs = sub.messages || [];
  const state = sub.state;
  return (
    <div className="flex flex-col gap-2 text-sm">
      <p className="font-medium">{tr(`customs.sub.state.${state}`, state)}</p>
      <p className="text-xs text-ink-tertiary">
        {[tr(`customs.sub.channel.${sub.channel}`, sub.channel),
          sub.prepared_at ? t("customs.sub.prepared", { time: dateTime(sub.prepared_at, locale) }) : null,
          sub.submitted_at ? t("customs.sub.submitted", { time: dateTime(sub.submitted_at, locale) }) : null,
          sub.xml_version ? t("customs.sub.template", { v: sub.xml_version }) : null].filter(Boolean).join(" · ")}
      </p>
      {sub.note && state === "awaiting_manual_filing" && <p className="text-xs text-ink-secondary">{sub.note}</p>}
      {sub.error && <p className="text-xs text-danger-fg">{sub.error}</p>}
      {msgs.length > 0 && (
        <div>
          <p className="mt-1 text-xs text-ink-tertiary">{t("customs.sub.messages")}</p>
          <ul className="mt-0.5 flex flex-col gap-1">
            {msgs.map((m) => (
              <li key={m.id} className="text-xs">
                <span className="font-medium">{tr(`customs.rr.event.${m.type}`, m.type)}</span>
                <span className="text-ink-tertiary"> · {dateTime(m.at, locale)}{m.recorded_by?.name ? ` · ${m.recorded_by.name}` : ""}</span>
                {m.mrn && <span className="ml-1 font-mono">{m.mrn}</span>}
                {m.note && <span className="block text-ink-secondary">{m.note}</span>}
                {m.document_id && (
                  <a className="inline-flex items-center gap-1 text-ink-accent hover:underline" href={fileUrl(`/documents/${m.document_id}/file`)} target="_blank" rel="noreferrer">
                    <FileText className="size-3" /> {t("action.open")}
                  </a>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function XmlPreview({ decl }) {
  const { t } = useT();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState({ xml: null, loading: false, error: null });
  const load = async () => {
    setState({ xml: null, loading: true, error: null });
    try {
      const r = await fetch(fileUrl(`/customs/${decl.id}/xml`), { credentials: "include" });
      if (!r.ok) throw new Error(`${t("state.error")} (${r.status})`);
      setState({ xml: await r.text(), loading: false, error: null });
    } catch (e) {
      setState({ xml: null, loading: false, error: e });
    }
  };
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && !state.xml) load();
  };
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" onClick={toggle} aria-expanded={open}>
          {open ? <ChevronDown /> : <ChevronRight />} {open ? t("customs.xml.hide") : t("customs.xml.show")}
        </Button>
        <Button variant="ghost" size="sm" asChild>
          <a href={fileUrl(`/customs/${decl.id}/xml?download=1`)}><Download /> {t("customs.action.xml")}</a>
        </Button>
      </div>
      {open && (
        <div className="mt-2">
          {state.loading && <p className="text-sm text-ink-tertiary">{t("state.loading")}</p>}
          {state.error && <ErrorState error={state.error} onRetry={load} compact />}
          {state.xml && (
            <pre className="surface-sunken max-h-96 overflow-auto rounded-lg p-3 font-mono text-[11px] leading-4 whitespace-pre">{state.xml}</pre>
          )}
        </div>
      )}
    </div>
  );
}
