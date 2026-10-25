import React from "react";
import { UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { PageHeader, Section } from "@/components/app/layout";
import { DataTable } from "@/components/app/DataTable";
import { Loadable } from "@/components/app/states";
import { Badge } from "@/components/ui/badge";
import { InviteDialog } from "@/features/InviteDialog";
import { apiPatch, useApi } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, relTime } from "@/lib/format";
import { tEnum, useT } from "@/lib/i18n";
import { toast } from "sonner";

export default function Team() {
  const { t, locale } = useT();
  const { user, can } = useAuth();
  const q = useApi("/team");
  const update = async (id, body) => {
    try { await apiPatch(`/team/${id}`, body); q.reload(); } catch (e) { toast.error(e.message); }
  };
  return (
    <div className="max-w-5xl">
      <PageHeader title={t("team.title")}
        actions={can("company.manage") && (
          <InviteDialog endpoint="/team/invite" roles={["customer_admin", "customer_user"]} title={t("team.invite_title")} onDone={q.reload}
            trigger={<Button><UserPlus />{t("team.invite")}</Button>} />
        )} />
      <Loadable q={q}>
        {(d) => (
          <>
            <DataTable rows={d.users} columns={[
              { key: "name", header: t("field.name"), cell: (u) => (
                <span className="flex items-center gap-2"><span className="font-medium">{u.name}</span>{u.id === user.id && <Badge>{t("team.you")}</Badge>}{u.disabled && <Badge variant="outline">{t("team.disabled")}</Badge>}</span>) },
              { key: "email", header: t("field.email"), cell: (u) => <span className="text-ink-secondary">{u.email}</span>, hideBelow: "md" },
              { key: "role", header: t("field.role"), cell: (u) => tEnum(t, "role", u.role) },
              { key: "last_login_at", header: t("team.last_login"), align: "right", cell: (u) => u.last_login_at ? relTime(u.last_login_at, locale) : "—", hideBelow: "md" },
              { key: "actions", header: "", sortable: false, align: "right", cell: (u) => can("company.manage") && u.id !== user.id && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild><Button variant="ghost" size="xs">{t("action.edit")}</Button></DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    {u.role === "customer_user" ? <DropdownMenuItem onSelect={() => update(u.id, { role: "customer_admin" })}>{t("team.make_admin")}</DropdownMenuItem>
                      : <DropdownMenuItem onSelect={() => update(u.id, { role: "customer_user" })}>{t("team.make_member")}</DropdownMenuItem>}
                    <DropdownMenuItem onSelect={() => update(u.id, { disabled: !u.disabled })}>{u.disabled ? t("team.enable") : t("team.disable")}</DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>) },
            ]} />
            {d.invites.length > 0 && (
              <Section title={t("team.pending")} className="mt-6">
                <DataTable rows={d.invites} rowKey="email" columns={[
                  { key: "email", header: t("field.email") },
                  { key: "role", header: t("field.role"), cell: (i) => tEnum(t, "role", i.role) },
                  { key: "created_at", header: t("field.created"), align: "right", cell: (i) => date(i.created_at, locale) },
                ]} />
              </Section>
            )}
          </>
        )}
      </Loadable>
    </div>
  );
}
