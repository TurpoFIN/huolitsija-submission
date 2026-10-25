import React from "react";
import MessagesPage from "@/features/messages/Inbox";

/** /ops/messages and /ops/messages/:id — customer conversations, AI reply drafts waiting for approval. */
export default function Messages() {
  return <MessagesPage mode="ops" />;
}
