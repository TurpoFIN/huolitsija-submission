import React from "react";
import { Check, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { PageHeader, Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { Loadable } from "@/components/app/states";
import { InviteDialog } from "@/features/InviteDialog";
import { apiPatch, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { toast } from "sonner";

const INTERNAL = ["ops_admin", "ops_agent", "customs_broker", "finance", "sales"];

export default function Staff() {
  const { t, locale } = useT();
  const { user } = useAuth();
  const q = useApi("/ops/users");
  const roles = useApi("/ops/roles");
  const carriers = useApi("/carriers?limit=500");
  const update = async (id, body) => { try { await apiPatch(`/ops/users/${id}`, body); q.reload(); } catch (e) { toast.error(e.message); } };
  const carrierList = carriers.data?.items || [];
  const carrierName = (id) => carrierList.find((c) => c.id === id)?.name;
  return (
    <div>
      <PageHeader title={t("staff.title")} actions={
        <InviteDialog endpoint="/ops/users/invite" roles={[...INTERNAL, "partner"]} carriers={carrierList} title={t("staff.invite_title")} onDone={q.reload}
          trigger={<Button><UserPlus />{t("staff.invite")}</Button>} />} />
      <Section title={t("staff.users")}>
        <Loadable q={q}>
          {(d) => (
            <DataTable rows={d.users} columns={[
              { key: "name", header: t("field.name"), cell: (u) => <span className="flex items-center gap-2"><span className="font-medium">{u.name}</span>{u.id === user.id && <Badge>{t("team.you")}</Badge>}{u.disabled && <Badge variant="outline">{t("team.disabled")}</Badge>}{u.demo && <Badge variant="outline">demo</Badge>}</span> },
              { key: "email", header: t("field.email"), cell: (u) => <span className="text-ink-secondary">{u.email}</span>, hideBelow: "md" },
              { key: "org_type", header: "", cell: (u) => t(`staff.type.${u.org_type}`), hideBelow: "lg" },
              { key: "role", header: t("field.role"), cell: (u) => <span>{tEnum(t, "role", u.role)}{u.carrier_id ? <span className="text-ink-tertiary"> · {carrierName(u.carrier_id) || "—"}</span> : null}</span> },
              { key: "last_login_at", header: t("team.last_login"), align: "right", cell: (u) => (u.last_login_at ? relTime(u.last_login_at, locale) : "—"), hideBelow: "md" },
              { key: "a", header: "", sortable: false, align: "right", cell: (u) => u.id !== user.id && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild><Button size="xs" variant="ghost">{t("action.edit")}</Button></DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    {u.org_type === "internal" && (<><DropdownMenuLabel className="text-xs text-ink-tertiary">{t("field.role")}</DropdownMenuLabel>
                      {INTERNAL.map((r) => <DropdownMenuItem key={r} disabled={r === u.role} onSelect={() => update(u.id, { role: r })}>{tEnum(t, "role", r)}</DropdownMenuItem>)}
                      <DropdownMenuSeparator /></>)}
                    <DropdownMenuItem onSelect={() => update(u.id, { disabled: !u.disabled })}>{u.disabled ? t("team.enable") : t("team.disable")}</DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>) },
            ]} />
          )}
        </Loadable>
      </Section>
      <Section title={t("staff.roles")}>
        <Loadable q={roles}>
          {(d) => (
            <>
            <div className="grid gap-3 md:hidden">{d.roles.map(r => <details key={r.id} className="rounded-lg border p-3"><summary className="cursor-pointer text-sm font-medium">{tEnum(t, "role", r.id)} · {r.permissions.length}</summary><ul className="mt-3 space-y-2">{d.permissions.map(p => <li key={p.id} className="flex items-start gap-2 text-sm"><span className="shrink-0" aria-label={t(r.permissions.includes(p.id) ? "review.permission_yes" : "review.permission_no")}>{r.permissions.includes(p.id) ? "✓" : "×"}</span><span className={r.permissions.includes(p.id) ? "" : "text-ink-tertiary"}>{t(`review.permission.${p.id}`)}</span></li>)}</ul></details>)}</div>
            <div className="hidden md:block"><Table containerClassName="rounded-lg">
              <TableHeader>
                <TableRow>
                  <TableHead className="sticky left-0 bg-[var(--bg-surface)]">{t("staff.permission")}</TableHead>
                  {d.roles.map((r) => <TableHead key={r.id} className="text-center">{tEnum(t, "role", r.id)}</TableHead>)}
                </TableRow>
              </TableHeader>
              <TableBody>
                {d.permissions.map((p) => (
                  <TableRow key={p.id}>
                    <TableCell className="sticky left-0 bg-[var(--bg-surface)] min-w-[220px]"><span className="text-sm">{t(`review.permission.${p.id}`)}</span></TableCell>
                    {d.roles.map((r) => <TableCell key={r.id} className="text-center">{r.permissions.includes(p.id) ? <Check className="mx-auto size-4" aria-label={t("review.permission_yes")} /> : <span className="text-ink-disabled" aria-label={t("review.permission_no")}>·</span>}</TableCell>)}
                  </TableRow>
                ))}
              </TableBody>
            </Table></div></>
          )}
        </Loadable>
      </Section>
    </div>
  );
}
