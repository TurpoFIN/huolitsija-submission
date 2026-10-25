import React from "react";
import MessagesPage from "@/features/messages/Inbox";

/** /portal/messages and /portal/messages/:id — the customer's conversations with Huolitsija. */
export default function Messages() {
  return <MessagesPage mode="customer" />;
}
