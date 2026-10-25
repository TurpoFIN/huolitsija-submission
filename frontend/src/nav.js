import {
  Activity, AlertOctagon, BarChart3, BookOpen, Bot, Briefcase, Building2, Cable, CheckSquare, FileText, Gauge, Handshake, Home, Inbox, KeyRound, Landmark, Map, MessageSquare, Package,
  Receipt, Route, Scale, Settings, ShieldAlert, ShieldCheck, Ship, Tags, Truck, Users, Wallet, Workflow,
} from "lucide-react";

export const PORTAL_NAV = [
  { items: [
    { to: "/portal", label: "nav.overview", icon: Home, end: true },
    { to: "/portal/quotes", label: "nav.quotes", icon: Tags },
    { to: "/portal/shipments", label: "nav.shipments", icon: Package },
    { to: "/portal/map", label: "nav.map", icon: Map },
    { to: "/portal/documents", label: "nav.documents", icon: FileText },
    { to: "/portal/customs", label: "nav.customs", icon: Landmark },
  ] },
  { label: "nav.group.money", items: [
    { to: "/portal/invoices", label: "nav.invoices", icon: Receipt },
    { to: "/portal/claims", label: "nav.claims", icon: Scale },
  ] },
  { items: [
    { to: "/portal/messages", label: "nav.messages", icon: MessageSquare },
    { to: "/portal/company", label: "nav.company", icon: Building2 },
    { to: "/portal/team", label: "nav.team", icon: Users },
    { to: "/portal/api", label: "nav.api", icon: KeyRound },
    { to: "/portal/settings", label: "nav.settings", icon: Settings },
  ] },
];

export const OPS_NAV = [
  { items: [
    { to: "/ops", label: "nav.today", icon: Gauge, end: true },
    { to: "/ops/approvals", label: "nav.approvals", icon: CheckSquare },
    { to: "/ops/demo", label: "nav.demoflow", icon: Workflow },
    { to: "/ops/insights", label: "nav.insights", icon: BarChart3 },
    { to: "/ops/shipments", label: "nav.shipments", icon: Package },
    { to: "/ops/execution", label: "nav.execution", icon: Truck },
    { to: "/ops/map", label: "nav.map", icon: Map },
    { to: "/ops/quotes", label: "nav.quotes", icon: Tags },
    { to: "/ops/commercial", label: "nav.commercial", icon: Briefcase },
    { to: "/ops/customs", label: "nav.customs", icon: Landmark },
    { to: "/ops/compliance", label: "nav.compliance", icon: ShieldAlert },
    { to: "/ops/exceptions", label: "nav.exceptions", icon: AlertOctagon },
    { to: "/ops/messages", label: "nav.messages", icon: Inbox },
    { to: "/ops/email", label: "nav.email", icon: Inbox },
  ] },
  { label: "nav.group.money", items: [
    { to: "/ops/finance", label: "nav.finance", icon: Landmark },
    { to: "/ops/billing", label: "nav.billing", icon: Wallet },
    { to: "/ops/claims", label: "nav.claims", icon: Scale },
  ] },
  { label: "nav.group.network", items: [
    { to: "/ops/customers", label: "nav.customers", icon: Building2 },
    { to: "/ops/carriers", label: "nav.carriers", icon: Truck },
    { to: "/ops/rates", label: "nav.rates", icon: Route },
    { to: "/ops/sourcing", label: "nav.sourcing", icon: Handshake },
    { to: "/ops/edi", label: "nav.edi", icon: Cable },
  ] },
  { label: "nav.group.admin", items: [
    { to: "/ops/autopilot", label: "nav.autopilot", icon: Activity },
    { to: "/ops/ai", label: "nav.ai", icon: Bot },
    { to: "/ops/team", label: "nav.staff", icon: ShieldCheck },
    { to: "/ops/runbook", label: "nav.runbook", icon: BookOpen },
    { to: "/ops/settings", label: "nav.settings", icon: Settings },
  ] },
];

export const PARTNER_NAV = [
  { items: [{ to: "/partner/appointments", label: "nav.appointments", icon: Truck }] },
  { items: [{ to: "/partner", label: "review.partner_jobs", icon: Ship, end: true }] },
  { items: [{ to: "/partner/settings", label: "nav.settings", icon: Settings }] },
];

/** Where "Profile" in the user menu goes: each area's settings page (profile, language, password). */
export function settingsPath(orgType) {
  return orgType === "internal" ? "/ops/settings" : orgType === "partner" ? "/partner/settings" : "/portal/settings";
}
