// Inbox list of e-mail threads: filter pills, search, rows. Same anatomy as the Messages inbox list.
import React from "react";
import { Link } from "react-router-dom";
import { Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState, Freshness } from "@/components/app/states";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { compactTime } from "@/features/messages/util";
import { FILTERS, oneLine } from "./strings";
import { LangBadge, OwnerBadge, ThreadStateBadge } from "./EmailThreadView";

function FilterPills({ value, onChange, counts }) {
  const { t } = useT();
  return (
    <div role="radiogroup" aria-label={t("email.filter.label")} className="flex flex-wrap gap-1.5">
      {FILTERS.map((f) => {
        const on = value === f;
        const n = counts?.[f];
        return (
          <button key={f} type="button" role="radio" aria-checked={on} onClick={() => onChange(f)}
            className={cn("inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 text-sm font-medium text-ink-secondary hover:bg-hover hover:text-foreground",
              on && "bg-secondary text-foreground shadow-[inset_0_0_0_0.5px_var(--border-default)]")}>
            {t(`email.filter.${f}`)}
            {n !== undefined && n !== null && <span className={cn("tabular text-xs", f === "owner" && n > 0 ? "text-warning-fg" : "text-ink-tertiary")}>{n}</span>}
          </button>
        );
      })}
    </div>
  );
}

/** One row: customer (+ ref), time; subject · preview; state and decision markers. */
export function EmailThreadRow({ item, selected, to }) {
  const { t, locale } = useT();
  const who = item.customer_name || item.customer_email || "—";
  const preview = oneLine(item.last?.preview);
  const prefix = item.last?.direction === "out" ? `${t("email.row.out_prefix")}: ` : "";
  return (
    <Link to={to} aria-current={selected ? "page" : undefined} data-email-row={item.id}
      className={cn("relative flex flex-col gap-0.5 rounded-lg px-3 py-2 outline-none hover:bg-hover focus-visible:ring-2 focus-visible:ring-[color:var(--ring)]",
        selected && "bg-[var(--bg-subtle)] shadow-[inset_0_0_0_0.5px_var(--border-default)] hover:bg-[var(--bg-subtle)]")}>
      <div className="flex items-center gap-2">
        <span className={cn("size-2 shrink-0 rounded-full", item.needs_owner ? "bg-warning" : "bg-transparent")} aria-hidden />
        <span className={cn("min-w-0 flex-1 truncate text-sm", item.needs_owner ? "font-semibold text-foreground" : "font-medium text-foreground")}>
          {who}
          {item.ref && <span className="ml-1.5 font-mono text-[11px] font-normal text-ink-tertiary">{item.ref}</span>}
        </span>
        <time dateTime={item.last_message_at} title={item.last_message_at} className="shrink-0 text-xs tabular text-ink-tertiary">
          {compactTime(item.last_message_at, locale, t)}
        </time>
      </div>
      <p className="truncate pl-4 text-xs text-ink-secondary">{item.subject || t("email.msg.no_subject")}</p>
      <div className="flex items-center gap-2 pl-4">
        <p className="min-w-0 flex-1 truncate text-xs text-ink-tertiary">
          {preview ? <>{prefix}{preview}</> : <span className="italic">{t("email.row.no_preview")}</span>}
        </p>
        <span className="flex shrink-0 items-center gap-1">
          {item.needs_owner ? <OwnerBadge /> : <ThreadStateBadge state={item.state} />}
          {item.language === "en" && <LangBadge lang="en" />}
        </span>
      </div>
    </Link>
  );
}

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-1 p-2" aria-busy="true">
      {Array.from({ length: 7 }).map((_, i) => (
        <div key={i} className="flex flex-col gap-1.5 px-3 py-2" style={{ opacity: 1 - i * 0.1 }}>
          <Skeleton className="h-4 w-3/4 rounded" />
          <Skeleton className="h-3 w-1/2 rounded" />
          <Skeleton className="h-3 w-full rounded" />
        </div>
      ))}
    </div>
  );
}

function ListEmpty({ filter, q, address, onShowAll }) {
  const { t } = useT();
  const showAll = <Button size="sm" variant="outline" onClick={onShowAll}>{t("email.empty.show_all")}</Button>;
  if (q) return <EmptyState className="m-2" title={t("email.empty.search", { q })} action={showAll} />;
  if (filter === "owner") return <EmptyState className="m-2" title={t("email.empty.owner")} body={t("email.empty.owner_body")} action={showAll} />;
  if (filter === "all") return <EmptyState className="m-2" title={t("email.empty.all")} body={address ? t("email.empty.all_body", { address }) : null} />;
  return <EmptyState className="m-2" title={t("email.empty.group")} action={showAll} />;
}

/**
 * list: useApi result of /email-channel/threads; items: already filtered; counts: per filter pill.
 * linkFor(id) -> row href.
 */
export default function EmailThreadList({ list, items, counts, filter, onFilter, search, onSearch, q, selectedId, linkFor, address, className }) {
  const { t } = useT();
  return (
    <aside aria-label={t("email.title")} className={cn("min-h-0 flex-col", className)}>
      <div className="flex shrink-0 flex-col gap-2 border-b border-[color:var(--border-subtle)] p-2.5">
        <FilterPills value={filter} onChange={onFilter} counts={counts} />
        <label className="surface-input flex h-8 items-center gap-2 rounded-lg px-2.5">
          <Search className="size-4 text-ink-tertiary" aria-hidden />
          <input data-quiet-input value={search} onChange={(e) => onSearch(e.target.value)} placeholder={t("email.search")}
            aria-label={t("email.search")} className="min-w-0 flex-1 bg-transparent text-md outline-none placeholder:text-ink-tertiary md:text-sm" />
          {search && (
            <button type="button" onClick={() => onSearch("")} aria-label={t("action.clear")} className="rounded-full p-0.5 hover:bg-hover">
              <X className="size-3.5" />
            </button>
          )}
        </label>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {list.error && list.data && <ErrorState compact className="m-2" error={{ message: t("state.failed_refresh") }} onRetry={list.reload} />}
        {list.loading && !list.data ? <ListSkeleton />
          : list.error && !list.data ? <ErrorState className="m-2" error={list.error} onRetry={list.reload} />
          : items.length === 0 ? <ListEmpty filter={filter} q={q} address={address} onShowAll={() => { onSearch(""); onFilter("all"); }} />
          : (
            <nav className="flex flex-col gap-0.5 p-1.5" aria-label={t(`email.filter.${filter}`)}>
              {items.map((it) => <EmailThreadRow key={it.id} item={it} selected={it.id === selectedId} to={linkFor(it.id)} />)}
            </nav>
          )}
      </div>
      <div className="flex shrink-0 items-center justify-end border-t border-[color:var(--border-subtle)] px-3 py-1">
        <Freshness at={list.updatedAt} onRefresh={list.reload} refreshing={list.refreshing} />
      </div>
    </aside>
  );
}
