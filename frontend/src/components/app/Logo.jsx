import React from "react";
import { cn } from "@/lib/utils";

/** Wordmark. The mark image is generated with Codex (public/brand/mark.png). */
export function Logo({ className, size = "sm" }) {
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <img src="/brand/mark.png" alt="" aria-hidden className={size === "lg" ? "size-7 rounded-md" : "size-5 rounded"} onError={(e) => { e.currentTarget.style.display = "none"; }} />
      <span className={cn("font-medium tracking-tight", size === "lg" ? "text-lg" : "text-sm")}>Huolitsija</span>
    </span>
  );
}
