// Customer API keys and webhooks. Decision: which systems can act for the company, and are our webhooks arriving.
import React, { useState } from "react";
import { BookOpen, Check, Copy, KeyRound, Plus, Webhook } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Metric, PageHeader, Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { EmptyState, ErrorState, Loadable } from "@/components/app/states";
import { StatusBadge } from "@/components/app/StatusBadge";
import { useConfirmChange } from "@/components/app/ConfirmChange";
import { apiDelete, apiPost, fileUrl, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { toast } from "sonner";

const DELIVERY_STATE = { pending: "scheduled", sending: "in-progress", delivered: "success", failed: "pending", dead: "failed",
  cancelled: "expired" };

function SecretDialog({ secret, title, body, onClose }) {
  const { t } = useT();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(secret); setCopied(true); } catch { setCopied(false); }
  };
  return (
    <Dialog open={!!secret} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>{body}</DialogDescription></DialogHeader>
        <div className="flex items-center gap-2">
          <code className="min-w-0 flex-1 break-all rounded-md border border-[color:var(--border-subtle)] bg-muted px-2 py-1.5 font-mono text-[13px]"
            data-testid="api-secret">{secret}</code>
          <Button variant="outline" size="sm" onClick={copy} aria-label={t("api.secret.copy")}>
            {copied ? <Check /> : <Copy />}{copied ? t("api.secret.copied") : t("api.secret.copy")}
          </Button>
        </div>
        <DialogFooter><Button onClick={onClose}>{t("api.secret.done")}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Choices({ options, value, onChange, label }) {
  const { locale } = useT();
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-sm font-medium">{label}</legend>
      {options.map((o) => (
        <label key={o.id} className="flex items-start gap-2 text-sm">
          <Checkbox checked={value.includes(o.id)} className="mt-0.5"
            onCheckedChange={(v) => onChange(v ? [...value, o.id] : value.filter((x) => x !== o.id))} />
          <span><span>{locale === "fi" ? o.fi : o.en}</span> <span className="font-mono text-xs text-ink-tertiary">{o.id}</span></span>
        </label>
      ))}
    </fieldset>
  );
}

function NewKeyDialog({ open, onOpenChange, scopes, onCreated }) {
  const { t } = useT();
  const [name, setName] = useState("");
  const [picked, setPicked] = useState(["quotes:read", "quotes:write", "bookings:write", "shipments:read", "tracking:read"]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const r = await apiPost("/portal/api-keys", { name, scopes: picked });
      onOpenChange(false); setName("");
      onCreated(r.secret);
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t("api.key.new")}</DialogTitle></DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <label className="text-sm font-medium" htmlFor="key-name">{t("api.key.name")}</label>
          <Input id="key-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} autoFocus />
          <p className="-mt-2 text-xs text-ink-tertiary">{t("api.key.name_hint")}</p>
          <Choices label={t("api.key.scopes")} value={picked} onChange={setPicked}
            options={scopes.map((s) => ({ id: s.scope, fi: s.fi, en: s.en }))} />
          {error && <ErrorState compact error={error} />}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={busy || !name.trim() || picked.length === 0}>{t("api.key.create")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function NewWebhookDialog({ open, onOpenChange, events, onCreated }) {
  const { t } = useT();
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [picked, setPicked] = useState(["*"]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const options = [{ id: "*", fi: t("api.webhook.all"), en: t("api.webhook.all") }, ...events.map((e) => ({ id: e.event, fi: e.fi, en: e.en }))];
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const r = await apiPost("/portal/webhooks", { url, description, events: picked.includes("*") ? ["*"] : picked });
      onOpenChange(false); setUrl(""); setDescription("");
      onCreated(r.secret);
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>{t("api.webhook.new")}</DialogTitle></DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <label className="text-sm font-medium" htmlFor="wh-url">{t("api.webhook.url")}</label>
          <Input id="wh-url" type="url" inputMode="url" placeholder="https://" value={url} onChange={(e) => setUrl(e.target.value)} autoFocus />
          <label className="text-sm font-medium" htmlFor="wh-desc">{t("api.webhook.description")}</label>
          <Input id="wh-desc" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={200} />
          <Choices label={t("api.webhook.events")} value={picked} onChange={setPicked} options={options} />
          {error && <ErrorState compact error={error} />}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>{t("action.cancel")}</Button>
            <Button type="submit" disabled={busy || !url.trim() || picked.length === 0}>{t("api.webhook.create")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function ApiKeys() {
  const { t, locale } = useT();
  const { can } = useAuth();
  const admin = can("company.manage");
  const q = useApi("/portal/api-keys", { enabled: admin });
  const [newKey, setNewKey] = useState(false);
  const [newHook, setNewHook] = useState(false);
  const [secret, setSecret] = useState(null); // {value, kind}
  const confirmation = useConfirmChange();

  const revoke = (k) => confirmation.confirm(async () => {
    try { await apiDelete(`/portal/api-keys/${k.id}`); q.reload(); } catch (e) { toast.error(e.message); }
  }, t("api.key.revoke_confirm", { name: k.name }));
  const remove = (w) => confirmation.confirm(async () => {
    try { await apiDelete(`/portal/webhooks/${w.id}`); q.reload(); } catch (e) { toast.error(e.message); }
  }, t("api.webhook.delete_confirm", { url: w.url }));
  const test = async (w) => {
    try {
      const { delivery } = await apiPost(`/portal/webhooks/${w.id}/test`);
      if (delivery.status === "delivered") toast.success(t("api.webhook.test_ok", { code: delivery.response_code }));
      else toast.error(t("api.webhook.test_failed", { error: delivery.last_error || delivery.status }));
      q.reload();
    } catch (e) { toast.error(e.message); }
  };
  const eventLabel = (e) => {
    const ev = (q.data?.events || []).find((x) => x.event === e);
    return ev ? (locale === "fi" ? ev.fi : ev.en) : e === "*" ? t("api.webhook.all") : e;
  };

  const docs = (
    <Button variant="outline" asChild>
      <a href={fileUrl("/v1/docs")} target="_blank" rel="noreferrer"><BookOpen />{t("api.docs")}</a>
    </Button>
  );

  if (!admin) {
    return (
      <div className="max-w-5xl">
        <PageHeader title={t("api.title")} actions={docs} />
        <EmptyState icon={KeyRound} title={t("api.title")} body={t("api.admin_only")} />
      </div>
    );
  }

  const active = (q.data?.keys || []).filter((k) => !k.revoked_at);
  const meta = q.data ? (
    <>
      <Metric value={active.length} label={t("api.meta.keys")} />
      <span className="text-ink-tertiary">·</span>
      <Metric value={(q.data.webhooks || []).length} label={t("api.meta.webhooks")} />
      <span className="text-ink-tertiary">·</span>
      <Metric value={q.data.rate_limit_per_min} label={t("api.meta.limit")} />
    </>
  ) : null;

  return (
    <div className="max-w-5xl">
      <PageHeader title={t("api.title")} meta={meta} actions={docs} />
      <Loadable q={q}>
        {(d) => (
          <>
            <Section title={t("api.keys")} actions={<Button size="sm" onClick={() => setNewKey(true)}><Plus />{t("api.key.new")}</Button>}>
              <DataTable rows={d.keys} defaultSort={{ key: "created_at", dir: "desc" }}
                empty={<EmptyState icon={KeyRound} title={t("api.key.empty_title")} body={t("api.key.empty_body")} />}
                rowClassName={(k) => (k.revoked_at ? "text-ink-tertiary" : "")}
                columns={[
                  { key: "name", header: t("api.key.name"), cell: (k) => (
                    <span className="flex flex-wrap items-center gap-2"><span className="font-medium">{k.name}</span>
                      {k.revoked_at && <Badge variant="outline">{t("api.key.revoked")}</Badge>}</span>) },
                  { key: "prefix", header: t("api.key.prefix"), cell: (k) => <span className="font-mono text-[13px]">{k.prefix}_…</span>, hideBelow: "md" },
                  { key: "scopes", header: t("api.key.scopes"), sortable: false, hideBelow: "lg", cell: (k) => (
                    <span className="flex flex-wrap gap-1">{k.scopes.map((s) => <Badge key={s} variant="outline" className="font-mono text-[11px]">{s}</Badge>)}</span>) },
                  { key: "last_used_at", header: t("api.key.last_used"), align: "right", cell: (k) => (k.last_used_at ? relTime(k.last_used_at, locale) : t("api.key.never")) },
                  { key: "created_at", header: t("api.key.created"), align: "right", hideBelow: "md", cell: (k) => date(k.created_at, locale) },
                  { key: "actions", header: "", sortable: false, align: "right", cell: (k) => !k.revoked_at && (
                    <Button variant="ghost" size="xs" onClick={() => revoke(k)}>{t("api.key.revoke")}</Button>) },
                ]} />
            </Section>

            <Section title={t("api.webhooks")} actions={<Button size="sm" variant="outline" onClick={() => setNewHook(true)}><Plus />{t("api.webhook.new")}</Button>}>
              <DataTable rows={d.webhooks}
                empty={<EmptyState icon={Webhook} title={t("api.webhook.empty_title")} body={t("api.webhook.empty_body")} />}
                columns={[
                  { key: "url", header: t("api.webhook.url"), cell: (w) => (
                    <span className="flex min-w-0 flex-col"><span className="break-all font-mono text-[13px]">{w.url}</span>
                      {w.description && <span className="text-xs text-ink-tertiary">{w.description}</span>}</span>) },
                  { key: "events", header: t("api.webhook.events"), sortable: false, hideBelow: "md", cell: (w) => (
                    <span className="text-sm text-ink-secondary">{w.events.map(eventLabel).join(", ")}</span>) },
                  { key: "last_status", header: t("api.webhook.last"), align: "right", hideBelow: "md", cell: (w) => (w.last_status
                    ? <StatusBadge size="sm" state={DELIVERY_STATE[w.last_status]} value={w.last_status} label={tEnum(t, "delivery", w.last_status)} />
                    : "—") },
                  { key: "actions", header: "", sortable: false, align: "right", cell: (w) => (
                    <span className="flex justify-end gap-1">
                      <Button variant="ghost" size="xs" onClick={() => test(w)}>{t("api.webhook.test")}</Button>
                      <Button variant="ghost" size="xs" onClick={() => remove(w)}>{t("api.webhook.delete")}</Button>
                    </span>) },
                ]} />
            </Section>

            {d.webhooks.length > 0 && (
              <Section title={t("api.deliveries")}>
                <DataTable rows={d.deliveries} dense
                  empty={<p className="py-3 text-sm text-ink-tertiary">{t("api.delivery.empty")}</p>}
                  columns={[
                    { key: "event", header: t("api.delivery.event"), cell: (r) => <span className="font-mono text-[13px]">{r.event}</span> },
                    { key: "status", header: t("api.delivery.status"), cell: (r) => (
                      <StatusBadge size="sm" state={DELIVERY_STATE[r.status]} value={r.status} label={tEnum(t, "delivery", r.status)} />) },
                    { key: "response_code", header: t("api.delivery.code"), align: "right", hideBelow: "md", cell: (r) => r.response_code ?? "—" },
                    { key: "attempts", header: t("api.delivery.attempts"), align: "right", hideBelow: "md" },
                    { key: "created_at", header: t("api.delivery.at"), align: "right", cell: (r) => relTime(r.created_at, locale) },
                  ]} />
              </Section>
            )}

            <NewKeyDialog open={newKey} onOpenChange={setNewKey} scopes={d.scopes}
              onCreated={(s) => { setSecret({ value: s, kind: "key" }); q.reload(); }} />
            <NewWebhookDialog open={newHook} onOpenChange={setNewHook} events={d.events}
              onCreated={(s) => { setSecret({ value: s, kind: "webhook" }); q.reload(); }} />
          </>
        )}
      </Loadable>
      <SecretDialog secret={secret?.value}
        title={secret?.kind === "webhook" ? t("api.webhook.secret_title") : t("api.secret.title")}
        body={secret?.kind === "webhook" ? t("api.webhook.secret_body") : t("api.secret.body")}
        onClose={() => setSecret(null)} />
      {confirmation.dialog}
    </div>
  );
}
