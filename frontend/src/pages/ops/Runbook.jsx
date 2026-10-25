// Ops: owner runbook (/ops/runbook). Decision: "what must the owner do to take this company live, and who answers for what?"
// The go-live runbook and its companion documents as shipped with this release (read-only), plus the printable PDF.
import React, { useMemo, useState } from "react";
import { BookOpen, Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/app/layout";
import { EmptyState, Loadable, LoadingRows } from "@/components/app/states";
import { Markdown } from "@/features/runbook/markdown";
import { fileUrl, useApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const docLabel = (t, d) => (t(`runbook.doc.${d.id}`) === `runbook.doc.${d.id}` ? d.title : t(`runbook.doc.${d.id}`));

function Doc({ id, files, onDoc }) {
  const q = useApi(`/runbook/${id}`);
  return (
    <Loadable q={q} skeleton={<LoadingRows rows={10} />}>
      {(d) => <Markdown text={d.markdown} files={files} onDoc={onDoc} />}
    </Loadable>
  );
}

export default function Runbook() {
  const { t } = useT();
  const q = useApi("/runbook");
  const [picked, setPicked] = useState(null);
  const items = useMemo(() => q.data?.items || [], [q.data]);
  const files = useMemo(() => Object.fromEntries(items.map((d) => [d.file, d.id])), [items]);
  const current = picked || items[0]?.id;
  const open = (id) => { setPicked(id); window.scrollTo({ top: 0 }); };
  return (
    <div>
      <PageHeader
        title={t("runbook.title")}
        meta={<span>{t("runbook.meta")}</span>}
        actions={q.data?.pdf && (
          <Button asChild variant="outline" size="sm">
            <a href={fileUrl("/runbook/pdf")} download><Download className="mr-1.5 size-4" aria-hidden />{t("runbook.pdf")}</a>
          </Button>
        )}
      />
      <Loadable q={q} isEmpty={(d) => !d.items.length}
        empty={<EmptyState icon={BookOpen} title={t("runbook.empty")} body={t("runbook.empty_body")} />}>
        {() => (
          <div className="grid gap-6 lg:grid-cols-[220px_minmax(0,1fr)]">
            <nav aria-label={t("runbook.docs")} className="flex gap-1 overflow-x-auto lg:flex-col">
              {items.map((d) => (
                <button key={d.id} type="button" onClick={() => open(d.id)} aria-current={d.id === current ? "page" : undefined}
                  className={cn("whitespace-nowrap rounded px-2 py-1.5 text-left text-sm lg:whitespace-normal",
                    d.id === current ? "bg-muted font-medium text-foreground" : "text-ink-secondary hover:text-foreground")}>
                  {docLabel(t, d)}
                </button>
              ))}
              <p className="mt-3 hidden text-xs text-ink-tertiary lg:block">{t("runbook.lang_note")}</p>
            </nav>
            <article className="min-w-0">
              {current && <Doc key={current} id={current} files={files} onDoc={open} />}
            </article>
          </div>
        )}
      </Loadable>
    </div>
  );
}
