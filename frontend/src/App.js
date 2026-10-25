import React, { Suspense, lazy } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { I18nProvider } from "@/lib/i18n";
import { AuthProvider, RequireAuth } from "@/lib/auth";
import { AppShell } from "@/components/app/AppShell";
import { LoadingRows } from "@/components/app/states";
import { OPS_NAV, PARTNER_NAV, PORTAL_NAV } from "@/nav";

const L = (f) => lazy(f);
// public
const Landing = L(() => import("@/pages/public/Landing"));
const InstantQuote = L(() => import("@/pages/public/InstantQuote"));
const Terms = L(() => import("@/pages/public/Terms"));
const Privacy = L(() => import("@/pages/public/Privacy"));
const Dpa = L(() => import("@/pages/public/Dpa"));
const Cookies = L(() => import("@/pages/public/Cookies"));
const CompanyInfo = L(() => import("@/pages/public/CompanyInfo"));
// auth
const Login = L(() => import("@/pages/auth/Login"));
const Signup = L(() => import("@/pages/auth/Signup"));
const VerifyEmail = L(() => import("@/pages/auth/VerifyEmail"));
const ForgotPassword = L(() => import("@/pages/auth/ForgotPassword"));
const ResetPassword = L(() => import("@/pages/auth/ResetPassword"));
const AcceptInvite = L(() => import("@/pages/auth/AcceptInvite"));
// portal
const POverview = L(() => import("@/pages/portal/Overview"));
const PQuotes = L(() => import("@/pages/portal/Quotes"));
const PQuoteNew = L(() => import("@/pages/portal/QuoteNew"));
const PQuoteDetail = L(() => import("@/pages/portal/QuoteDetail"));
const PBookingNew = L(() => import("@/pages/portal/BookingNew"));
const PShipments = L(() => import("@/pages/portal/Shipments"));
const PShipmentDetail = L(() => import("@/pages/portal/ShipmentDetail"));
const PApiKeys = L(() => import("@/pages/portal/ApiKeys"));
const PMap = L(() => import("@/pages/portal/MapPage"));
const PDocuments = L(() => import("@/pages/portal/Documents"));
const PCustoms = L(() => import("@/pages/portal/Customs"));
const PCustomsDetail = L(() => import("@/pages/portal/CustomsDetail"));
const PInvoices = L(() => import("@/pages/portal/Invoices"));
const PInvoiceDetail = L(() => import("@/pages/portal/InvoiceDetail"));
const PMessages = L(() => import("@/pages/portal/Messages"));
const PClaims = L(() => import("@/pages/portal/Claims"));
const PCompany = L(() => import("@/pages/portal/Company"));
const PTeam = L(() => import("@/pages/portal/Team"));
const PSettings = L(() => import("@/pages/portal/Settings"));
// ops
const OToday = L(() => import("@/pages/ops/Today"));
const OApprovals = L(() => import("@/pages/ops/Approvals"));
const OShipments = L(() => import("@/pages/ops/Shipments"));
const OShipmentDetail = L(() => import("@/pages/ops/ShipmentDetail"));
const OMap = L(() => import("@/pages/ops/MapPage"));
const OQuotes = L(() => import("@/pages/ops/Quotes"));
const OQuoteDetail = L(() => import("@/pages/ops/QuoteDetail"));
const OExecution = L(() => import("@/pages/ops/Execution"));
const ExecutionReply = L(() => import("@/features/execution/Replies"));
const PartnerSlots = L(() => import("@/features/execution/Replies").then(m => ({ default: m.PartnerSlots })));
const OCommercial = L(() => import("@/pages/ops/Commercial"));
const OCustoms = L(() => import("@/pages/ops/Customs"));
const OCustomsDetail = L(() => import("@/pages/ops/CustomsDetail"));
const OCustomsExport = L(() => import("@/pages/ops/CustomsExport"));
const OCustomsTransit = L(() => import("@/pages/ops/CustomsTransit"));
const OCustomsEns = L(() => import("@/pages/ops/CustomsEns"));
const OCustomsReference = L(() => import("@/pages/ops/CustomsReference"));
const OCompliance = L(() => import("@/pages/ops/Compliance"));
const OExceptions = L(() => import("@/pages/ops/Exceptions"));
const OEmail = L(() => import("@/pages/ops/EmailInbox"));
const OMessages = L(() => import("@/pages/ops/Messages"));
const OBilling = L(() => import("@/pages/ops/Billing"));
const OInvoiceDetail = L(() => import("@/pages/ops/InvoiceDetail"));
const OClaims = L(() => import("@/pages/ops/Claims"));
const OCustomers = L(() => import("@/pages/ops/Customers"));
const OCustomerDetail = L(() => import("@/pages/ops/CustomerDetail"));
const OCarriers = L(() => import("@/pages/ops/Carriers"));
const OCarrierDetail = L(() => import("@/pages/ops/CarrierDetail"));
const ORates = L(() => import("@/pages/ops/Rates"));
const OSourcing = L(() => import("@/pages/ops/Sourcing"));
const OAiRuns = L(() => import("@/pages/ops/AiRuns"));
const OAutopilot = L(() => import("@/pages/ops/Autopilot"));
const OStaff = L(() => import("@/pages/ops/Staff"));
const OSettings = L(() => import("@/pages/ops/Settings"));
const OFinance = L(() => import("@/pages/ops/finance/Finance"));
const OInsights = L(() => import("@/pages/ops/Insights"));
const OEdi = L(() => import("@/pages/ops/Edi"));
const ORunbook = L(() => import("@/pages/ops/Runbook"));
const ODemoRuns = L(() => import("@/features/demoflow/DemoRuns"));
const ODemoFlow = L(() => import("@/features/demoflow/DemoFlowPage"));
const PublicFlow = L(() => import("@/features/demoflow/PublicFlowPage"));
// partner
const PartnerJobs = L(() => import("@/pages/partner/Jobs"));
const PartnerJobDetail = L(() => import("@/pages/partner/JobDetail"));
const PartnerSettings = L(() => import("@/pages/partner/Settings"));

const Fallback = () => <div className="p-4"><LoadingRows rows={4} /></div>;

function Shell({ area, groups, orgTypes }) {
  return (
    <RequireAuth orgTypes={orgTypes}>
      <AppShell area={area} groups={groups}>
        <Suspense fallback={<Fallback />}>
          <Outlet />
        </Suspense>
      </AppShell>
    </RequireAuth>
  );
}

export default function App() {
  return (
    <I18nProvider>
      <BrowserRouter>
        <AuthProvider>
          <Suspense fallback={<Fallback />}>
            <Routes>
              <Route path="/slot-reply" element={<ExecutionReply />} />
              <Route path="/warehouse-reply" element={<ExecutionReply warehouse />} />
              <Route path="/" element={<Landing />} />
              <Route path="/quote" element={<InstantQuote />} />
              <Route path="/track/:runId" element={<Suspense fallback={<Fallback />}><PublicFlow /></Suspense>} />
              <Route path="/terms" element={<Terms />} />
              <Route path="/privacy" element={<Privacy />} />
              <Route path="/dpa" element={<Dpa />} />
              <Route path="/cookies" element={<Cookies />} />
              <Route path="/company" element={<CompanyInfo />} />
              <Route path="/login" element={<Login />} />
              <Route path="/signup" element={<Signup />} />
              <Route path="/verify-email" element={<VerifyEmail />} />
              <Route path="/forgot-password" element={<ForgotPassword />} />
              <Route path="/reset-password" element={<ResetPassword />} />
              <Route path="/invite/:token" element={<AcceptInvite />} />
              <Route path="/ops/demo/:runId" element={<RequireAuth orgTypes={["internal"]}><Suspense fallback={<Fallback />}><ODemoFlow /></Suspense></RequireAuth>} />

              <Route path="/portal" element={<Shell area="portal" groups={PORTAL_NAV} orgTypes={["customer"]} />}>
                <Route index element={<POverview />} />
                <Route path="quotes" element={<PQuotes />} />
                <Route path="quotes/new" element={<PQuoteNew />} />
                <Route path="quotes/:id" element={<PQuoteDetail />} />
                <Route path="bookings/new" element={<PBookingNew />} />
                <Route path="shipments" element={<PShipments />} />
                <Route path="shipments/:id" element={<PShipmentDetail />} />
                <Route path="map" element={<PMap />} />
                <Route path="documents" element={<PDocuments />} />
                <Route path="customs" element={<PCustoms />} />
                <Route path="customs/:id" element={<PCustomsDetail />} />
                <Route path="invoices" element={<PInvoices />} />
                <Route path="invoices/:id" element={<PInvoiceDetail />} />
                <Route path="messages" element={<PMessages />} />
                <Route path="messages/:id" element={<PMessages />} />
                <Route path="claims" element={<PClaims />} />
                <Route path="company" element={<PCompany />} />
                <Route path="team" element={<PTeam />} />
                <Route path="api" element={<PApiKeys />} />
                <Route path="settings" element={<PSettings />} />
              </Route>

              <Route path="/ops" element={<Shell area="ops" groups={OPS_NAV} orgTypes={["internal"]} />}>
                <Route index element={<OToday />} />
                <Route path="approvals" element={<OApprovals />} />
                <Route path="demo" element={<ODemoRuns />} />
                <Route path="shipments" element={<OShipments />} />
                <Route path="shipments/:id" element={<OShipmentDetail />} />
                <Route path="map" element={<OMap />} />
                <Route path="quotes" element={<OQuotes />} />
                <Route path="quotes/:id" element={<OQuoteDetail />} />
                <Route path="execution" element={<OExecution />} />
                <Route path="commercial/*" element={<OCommercial />} />
                <Route path="customs" element={<OCustoms />} />
                <Route path="customs/:id" element={<OCustomsDetail />} />
                <Route path="customs/export" element={<OCustomsExport />} />
                <Route path="customs/export/:id" element={<OCustomsExport />} />
                <Route path="customs/transit" element={<OCustomsTransit />} />
                <Route path="customs/transit/:id" element={<OCustomsTransit />} />
                <Route path="customs/ens" element={<OCustomsEns />} />
                <Route path="customs/ens/:id" element={<OCustomsEns />} />
                <Route path="customs/taric" element={<OCustomsReference />} />
                <Route path="customs/licences" element={<OCustomsReference />} />
                <Route path="customs/controls" element={<OCustomsReference />} />
                <Route path="compliance" element={<OCompliance />} />
                <Route path="exceptions" element={<OExceptions />} />
                <Route path="email" element={<OEmail />} />
                <Route path="messages" element={<OMessages />} />
                <Route path="messages/:id" element={<OMessages />} />
                <Route path="billing" element={<OBilling />} />
                <Route path="billing/:id" element={<OInvoiceDetail />} />
                <Route path="claims" element={<OClaims />} />
                <Route path="customers" element={<OCustomers />} />
                <Route path="customers/:id" element={<OCustomerDetail />} />
                <Route path="carriers" element={<OCarriers />} />
                <Route path="carriers/:id" element={<OCarrierDetail />} />
                <Route path="rates" element={<ORates />} />
                <Route path="sourcing" element={<OSourcing />} />
                <Route path="autopilot" element={<OAutopilot />} />
                <Route path="ai" element={<OAiRuns />} />
                <Route path="team" element={<OStaff />} />
                <Route path="settings" element={<OSettings />} />
                <Route path="finance/*" element={<OFinance />} />
                <Route path="insights" element={<OInsights />} />
                <Route path="edi" element={<OEdi />} />
                <Route path="runbook" element={<ORunbook />} />
              </Route>

              <Route path="/partner" element={<Shell area="partner" groups={PARTNER_NAV} orgTypes={["partner"]} />}>
                <Route index element={<PartnerJobs />} />
                <Route path="appointments" element={<PartnerSlots />} />
                <Route path="shipments/:id" element={<PartnerJobDetail />} />
                <Route path="settings" element={<PartnerSettings />} />
              </Route>
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
          <Toaster position="bottom-right" />
        </AuthProvider>
      </BrowserRouter>
    </I18nProvider>
  );
}
