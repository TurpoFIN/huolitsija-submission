import * as React from "react";
import { cva } from "class-variance-authority";
import { cn } from "@/lib/utils";

// Neutral pill for labels/counts. Workflow states use components/app/StatusBadge instead.
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2 h-5 text-[11px] font-medium whitespace-nowrap",
  {
    variants: {
      variant: {
        default: "bg-secondary text-ink-secondary",
        outline: "shadow-[inset_0_0_0_0.5px_var(--border-strong)] text-ink-secondary",
        solid: "bg-primary text-primary-foreground",
        agent: "agent-mark",
      },
    },
    defaultVariants: { variant: "default" },
  }
);

function Badge({ className, variant, ...props }) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
