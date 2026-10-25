// Ops: every customer's shipments, needs-action first, list or board. Owned by V1.
import React from "react";
import ShipmentList from "@/features/shipments/ShipmentList";

export default function Shipments() {
  return <ShipmentList scope="ops" />;
}
