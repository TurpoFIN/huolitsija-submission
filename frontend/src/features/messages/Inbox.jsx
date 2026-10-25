import React, { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { CornerDownLeft, Loader2, MessageSquare, Plus, Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader, Metric } from "@/components/app/layout";
import { AgentMark, StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState, ErrorState, Freshness } from "@/components/app/states";
import { useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import NewThreadDialog from "./NewThreadDialog";
import ThreadView from "./ThreadView";
import { compactTime, draftingFresh, inboxPath, tn } from "./util";

const FILTERS = {
  ops: ["needs_reply", "ai_pending", "unread", "all", "closed"],
  customer: ["all", "unread", "closed"],
};
const DEFAULT_FILTER = { ops: "needs_reply", customer: "all" };

function FilterPills({ mode, value, onChange, counts }) {
  const { t } = useT();
  return (
    <div role="radiogroup" aria-label={t("messages.filter.label")} className="flex flex-wrap gap-1.5">
      {FILTERS[mode].map((f) => {
        const on = value === f;
        const n = counts?.[f];
        return (
          <button key={f} type="button" role="radio" aria-checked={on} onClick={() => onChange(f)}
            className={cn("inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 text-sm font-medium text-ink-secondary hover:bg-hover hover:text-foreground",
              on && "bg-secondary text-foreground shadow-[inset_0_0_0_0.5px_var(--border-default)]")}>
            {t(`messages.filter.${f}`)}
            {n !== undefined && n !== null && <span className="tabular text-xs text-ink-tertiary">{n}</span>}
          </button>
        );
      })}
    </div>
  );
}

function previewPrefix(mode, item, t) {
  const a = item.last_author_type;
  if (mode === "customer") return a === "customer" ? `${t("messages.row.you")}: ` : "";
  if (a === "ops") return `${t("messages.role.ops")}: `;
  if (a === "ai") return `${t("messages.role.ai")}: `;
  return "";
}

/** One inbox row: unread dot, subject/ref (+ company for ops), one-line preview, time, small indicators. */
export function ThreadRow({ item, mode, selected, to }) {
  const { t, locale } = useT();
  const unread = (mode === "ops" ? item.unread_ops : item.unread_customer) > 0;
  const title = item.subject || item.shipment_reference || "—";
  const showRef = item.shipment_reference && item.shipment_reference !== title;
  const preview = item.last_message_preview && item.last_message_preview !== "📎" ? item.last_message_preview
    : item.last_message_preview === "📎" ? t("messages.row.attachment") : null;
  return (
    <Link to={to} aria-current={selected ? "page" : undefined} data-thread-row={item.id}
      className={cn("relative flex flex-col gap-0.5 rounded-lg px-3 py-2 outline-none hover:bg-hover focus-visible:ring-2 focus-visible:ring-[color:var(--ring)]",
        selected && "bg-[var(--bg-subtle)] shadow-[inset_0_0_0_0.5px_var(--border-default)] hover:bg-[var(--bg-subtle)]")}>
      <div className="flex items-center gap-2">
        <span className={cn("size-2 shrink-0 rounded-full", unread ? "bg-brand" : "bg-transparent")} aria-hidden />
        <span className={cn("min-w-0 flex-1 truncate text-sm", unread ? "font-semibold text-foreground" : "font-medium text-ink-secondary")}>
          {title}
          {showRef && <span className="ml-1.5 text-xs font-normal tabular text-ink-tertiary">{item.shipment_reference}</span>}
        </span>
        {unread && <span className="sr-only">{t("messages.filter.unread")}</span>}
        <time dateTime={item.last_message_at} title={item.last_message_at} className="shrink-0 text-xs tabular text-ink-tertiary">
          {compactTime(item.last_message_at, locale, t)}
        </time>
      </div>
      <div className="flex items-center gap-2 pl-4">
        <p className="min-w-0 flex-1 truncate text-xs text-ink-tertiary">
          {mode === "ops" && item.company_name && <span className="text-ink-secondary">{item.company_name} · </span>}
          {preview ? <>{previewPrefix(mode, item, t)}{preview}</> : <span className="italic">{t("messages.row.no_preview")}</span>}
        </p>
        {mode === "ops" && (
          <span className="flex shrink-0 items-center gap-1">
            {item.ai_status === "drafting" && !item.pending_ai_count && item.last_author_type === "customer" && draftingFresh(item.last_message_at) && (
              <span className="inline-flex items-center gap-1 text-[11px] text-ink-tertiary" title={t("messages.row.drafting")}>
                <Loader2 className="size-3 animate-spin" aria-hidden /><span className="sr-only">{t("messages.row.drafting")}</span>
              </span>
            )}
            {item.pending_ai_count > 0 && <AgentMark label={t("messages.row.ai_pending")} title={t("messages.row.ai_pending_title")} />}
            {item.ai_status === "needs_person" && item.needs_reply && (
              <StatusBadge size="sm" state="pending" icon={false} label={t("messages.row.needs_person")} />
            )}
            {item.needs_reply && item.status === "open" && (
              <CornerDownLeft className="size-3.5 text-warning-fg" aria-label={t("messages.row.needs_reply")} />
            )}
          </span>
        )}
        {item.status === "closed" && <StatusBadge size="sm" state="expired" icon={false} label={t("messages.row.closed")} />}
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
          <Skeleton className="h-3 w-full rounded" />
        </div>
      ))}
    </div>
  );
}

function ListEmpty({ mode, filter, q, onShowAll, onNew }) {
  const { t } = useT();
  if (q) return <EmptyState className="m-2" title={t("messages.empty.search", { q })} action={<Button size="sm" variant="outline" onClick={onShowAll}>{t("messages.empty.show_all")}</Button>} />;
  if (filter === "all") {
    return mode === "ops"
      ? <EmptyState className="m-2" title={t("messages.empty.all_ops")} action={<Button size="sm" onClick={onNew}><Plus /> {t("messages.new.ops")}</Button>} />
      : <EmptyState className="m-2" title={t("messages.empty.all_customer")} body={t("messages.empty.all_customer_body")} action={<Button size="sm" onClick={onNew}><Plus /> {t("messages.new.customer")}</Button>} />;
  }
  const body = filter === "needs_reply" ? t("messages.empty.needs_reply_body") : filter === "ai_pending" ? t("messages.empty.ai_pending_body") : null;
  return (
    <EmptyState className="m-2" title={t(`messages.empty.${filter}`)} body={body}
      action={<Button size="sm" variant="outline" onClick={onShowAll}>{t("messages.empty.show_all")}</Button>} />
  );
}

/** Decision line: ops "N odottaa vastausta · M AI-luonnosta hyväksyttävänä"; customer "N lukematonta". */
function Decision({ mode, counts, filterLink }) {
  const { t } = useT();
  if (!counts) return <Skeleton className="h-5 w-64 rounded" />;
  if (mode === "ops") {
    const nr = counts.needs_reply || 0;
    const ap = counts.ai_pending || 0;
    if (!nr && !ap) return <span>{t("messages.metric.none_ops")}</span>;
    return (
      <>
        <Metric value={nr} label={tn(t, "messages.metric.needs_reply", nr)} tone={nr ? "warning" : undefined} to={filterLink("needs_reply")} />
        <span className="text-ink-tertiary" aria-hidden>·</span>
        <Metric value={ap} label={tn(t, "messages.metric.ai_pending", ap)} to={filterLink("ai_pending")} />
      </>
    );
  }
  const un = counts.unread || 0;
  if (!un) return <span>{t("messages.metric.none_customer")}</span>;
  return <Metric value={un} label={tn(t, "messages.metric.unread", un)} to={filterLink("unread")} />;
}

/**
 * Linear-style two-pane inbox shared by /portal/messages and /ops/messages.
 * 390 px: one pane (list, or the thread selected by /messages/:id with a back button).
 */
export default function MessagesPage({ mode = "customer" }) {
  const { t } = useT();
  const { can } = useAuth();
  const { id } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const filter = FILTERS[mode].includes(params.get("f")) ? params.get("f") : DEFAULT_FILTER[mode];
  const [search, setSearch] = useState("");
  const [qDebounced, setQDebounced] = useState("");
  const [newOpen, setNewOpen] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => setQDebounced(search.trim()), 250);
    return () => clearTimeout(timer);
  }, [search]);

  const path = `/messages/threads?filter=${filter}${qDebounced ? `&q=${encodeURIComponent(qDebounced)}` : ""}`;
  const list = useApi(path, { pollMs: 30000 });
  const items = useMemo(() => list.data?.items || [], [list.data]);
  const base = inboxPath(mode);
  const qs = params.toString() ? `?${params.toString()}` : "";
  const linkFor = (tid) => `${base}/${tid}${qs}`;
  const filterLink = (f) => `${id ? `${base}/${id}` : base}?f=${f}`;
  const setFilter = (f) => setParams((p) => { const n = new URLSearchParams(p); n.set("f", f); return n; }, { replace: true });
  const canCreate = mode !== "ops" || can("messages.ops");

  // j / k move through the list (not while typing).
  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = (e.target?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || e.target?.isContentEditable) return;
      if (e.key !== "j" && e.key !== "k") return;
      if (!items.length) return;
      const idx = items.findIndex((x) => x.id === id);
      const next = e.key === "j" ? Math.min(idx + 1, items.length - 1) : Math.max(idx - 1, 0);
      const target = items[idx === -1 ? 0 : next];
      if (target && target.id !== id) {
        e.preventDefault();
        navigate(linkFor(target.id));
        setTimeout(() => document.querySelector(`[data-thread-row="${target.id}"]`)?.scrollIntoView({ block: "nearest" }), 0);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, id, qs]);

  // Desktop: open the first conversation instead of leaving the reading pane empty (390 px keeps the list).
  useEffect(() => {
    if (id || !items.length) return;
    if (typeof window !== "undefined" && window.matchMedia && !window.matchMedia("(min-width: 768px)").matches) return;
    navigate(linkFor(items[0].id), { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, items]);

  const onThreadChanged = (kind, tid) => {
    if (kind === "read") {
      const key = mode === "ops" ? "unread_ops" : "unread_customer";
      list.setData((d) => {
        if (!d) return d;
        const was = d.items.find((x) => x.id === tid)?.[key] > 0;
        return {
          ...d,
          items: d.items.map((x) => (x.id === tid ? { ...x, [key]: 0 } : x)),
          counts: was && d.counts ? { ...d.counts, unread: Math.max(0, (d.counts.unread || 0) - 1) } : d.counts,
        };
      });
      return;
    }
    list.reload();
  };

  const onCreated = (r) => {
    setNewOpen(false);
    list.reload();
    if (r?.thread?.id) navigate(`${base}/${r.thread.id}${qs}`);
  };

  const newLabel = mode === "ops" ? t("messages.new.ops") : t("messages.new.customer");

  return (
    <div className="flex h-[calc(100dvh-88px)] flex-col md:h-[calc(100dvh-104px)]">
      <PageHeader className={cn("mb-3 shrink-0", id && "hidden md:flex")} title={t("messages.title")}
        meta={<Decision mode={mode} counts={list.data?.counts} filterLink={filterLink} />}
        actions={canCreate ? <Button size="sm" onClick={() => setNewOpen(true)}><Plus /> {newLabel}</Button> : null} />
      <div className="flex min-h-0 flex-1 overflow-hidden surface-flat rounded-xl">
        <aside aria-label={t("messages.title")}
          className={cn("min-h-0 w-full flex-col md:w-[320px] md:shrink-0 md:border-r md:border-[color:var(--border-subtle)] lg:w-[380px]", id ? "hidden md:flex" : "flex")}>
          <div className="flex shrink-0 flex-col gap-2 border-b border-[color:var(--border-subtle)] p-2.5">
            <FilterPills mode={mode} value={filter} onChange={setFilter} counts={list.data?.counts} />
            <label className="surface-input flex h-8 items-center gap-2 rounded-lg px-2.5">
              <Search className="size-4 text-ink-tertiary" aria-hidden />
              <input data-quiet-input value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t("messages.search")}
                aria-label={t("messages.search")} className="min-w-0 flex-1 bg-transparent text-md outline-none placeholder:text-ink-tertiary md:text-sm" />
              {search && (
                <button type="button" onClick={() => setSearch("")} aria-label={t("action.clear")} className="rounded-full p-0.5 hover:bg-hover">
                  <X className="size-3.5" />
                </button>
              )}
            </label>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {list.error && list.data && <ErrorState compact className="m-2" error={{ message: t("state.failed_refresh") }} onRetry={list.reload} />}
            {list.loading && !list.data ? <ListSkeleton />
              : list.error && !list.data ? <ErrorState className="m-2" error={list.error} onRetry={list.reload} />
              : items.length === 0 ? (
                <ListEmpty mode={mode} filter={filter} q={qDebounced} onNew={() => setNewOpen(true)}
                  onShowAll={() => { setSearch(""); setFilter("all"); }} />
              ) : (
                <nav className="flex flex-col gap-0.5 p-1.5" aria-label={t(`messages.filter.${filter}`)}>
                  {items.map((it) => <ThreadRow key={it.id} item={it} mode={mode} selected={it.id === id} to={linkFor(it.id)} />)}
                </nav>
              )}
          </div>
          <div className="flex shrink-0 items-center justify-end border-t border-[color:var(--border-subtle)] px-3 py-1">
            <Freshness at={list.updatedAt} onRefresh={list.reload} refreshing={list.refreshing} />
          </div>
        </aside>
        <section className={cn("min-h-0 min-w-0 flex-1 flex-col", id ? "flex" : "hidden md:flex")}>
          {id ? (
            <ThreadView key={id} threadId={id} mode={mode} backTo={`${base}${qs}`} onChanged={onThreadChanged} />
          ) : (
            <div className="flex flex-1 items-center justify-center p-6">
              <div className="flex max-w-sm flex-col items-center gap-2 text-center">
                <MessageSquare className="size-5 text-ink-tertiary" aria-hidden />
                <p className="text-sm font-medium">{t("messages.select")}</p>
                <p className="text-sm text-ink-tertiary">{t("messages.select_body")}</p>
                {items.length > 0 && <p className="text-xs text-ink-tertiary"><kbd className="rounded border px-1 font-mono">j</kbd> / <kbd className="rounded border px-1 font-mono">k</kbd></p>}
              </div>
            </div>
          )}
        </section>
      </div>
      {canCreate && <NewThreadDialog mode={mode} open={newOpen} onOpenChange={setNewOpen} onCreated={onCreated} />}
    </div>
  );
}
