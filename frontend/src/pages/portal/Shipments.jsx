// Portal: the customer's shipments. Owned by V1.
import React from "react";
import ShipmentList from "@/features/shipments/ShipmentList";

export default function Shipments() {
  return <ShipmentList scope="customer" />;
}
