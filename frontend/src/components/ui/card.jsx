import * as React from "react";
import { cn } from "@/lib/utils";

// Raised surface, 8.4px radius, 16px rhythm. Use only when a boundary improves hierarchy.
const Card = React.forwardRef(({ className, size, ...props }, ref) => (
  <div ref={ref} data-slot="card" className={cn("surface-raised rounded-xl bg-card text-card-foreground", size === "sm" ? "p-3" : "p-4", className)} {...props} />
));
Card.displayName = "Card";
const CardHeader = React.forwardRef(({ className, ...props }, ref) => (
  <div ref={ref} className={cn("flex items-start justify-between gap-3 mb-3", className)} {...props} />
));
CardHeader.displayName = "CardHeader";
const CardTitle = React.forwardRef(({ className, ...props }, ref) => (
  <h3 ref={ref} className={cn("text-sm font-medium leading-5", className)} {...props} />
));
CardTitle.displayName = "CardTitle";
const CardDescription = React.forwardRef(({ className, ...props }, ref) => (
  <p ref={ref} className={cn("text-sm text-ink-tertiary", className)} {...props} />
));
CardDescription.displayName = "CardDescription";
const CardContent = React.forwardRef(({ className, ...props }, ref) => <div ref={ref} className={cn(className)} {...props} />);
CardContent.displayName = "CardContent";
const CardFooter = React.forwardRef(({ className, ...props }, ref) => (
  <div ref={ref} className={cn("flex items-center gap-2 mt-4", className)} {...props} />
));
CardFooter.displayName = "CardFooter";

export { Card, CardHeader, CardFooter, CardTitle, CardDescription, CardContent };
