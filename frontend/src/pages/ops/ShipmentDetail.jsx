// Ops: one shipment with actions (status, milestones, holds, cancel, legs, carriers) and internal notes. Owned by V1.
import React from "react";
import ShipmentDetailView from "@/features/shipments/ShipmentDetailView";
import InsurancePanel from "@/features/commercial/InsurancePanel";

import EmailPanel from "@/features/email/Thread";

import ShipmentExecution from "@/features/execution/ShipmentExecution";

export default function ShipmentDetail() {
  return <><ShipmentDetailView scope="ops" /><InsurancePanel /><EmailPanel /><div className="mt-4"><ShipmentExecution /></div></>;
}
