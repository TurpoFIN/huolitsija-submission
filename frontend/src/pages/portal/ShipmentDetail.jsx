// Portal: one shipment with milestones, legs, map and the documents/customs/invoices/messages panels. Owned by V1.
import React from "react";
import ShipmentDetailView from "@/features/shipments/ShipmentDetailView";

export default function ShipmentDetail() {
  return <ShipmentDetailView scope="customer" />;
}
