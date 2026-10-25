import React from "react";
import { Routes, Route } from "react-router-dom";
import FinanceLayout from "./FinanceLayout";
import Overview from "./Overview";
import Pnl from "./Pnl";
import Accruals from "./Accruals";
import CarrierInvoices from "./CarrierInvoices";
import CarrierInvoiceDetail from "./CarrierInvoiceDetail";
import Payables from "./Payables";
import Collections from "./Collections";
import Fx from "./Fx";
import Vat from "./Vat";
import Ledger from "./Ledger";
export default function Finance() { return <Routes><Route element={<FinanceLayout />}>
<Route index element={<Overview />} /><Route path="pnl" element={<Pnl />} />
<Route path="accruals" element={<Accruals />} /><Route path="carrier-invoices" element={<CarrierInvoices />} />
<Route path="carrier-invoices/:id" element={<CarrierInvoiceDetail />} />
<Route path="payables" element={<Payables />} /><Route path="payment-runs/:id" element={<Payables />} />
<Route path="collections" element={<Collections />} /><Route path="fx" element={<Fx />} />
<Route path="vat" element={<Vat />} /><Route path="ledger" element={<Ledger />} />
</Route></Routes>; }
