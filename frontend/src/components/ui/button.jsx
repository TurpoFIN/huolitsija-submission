import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva } from "class-variance-authority";
import { cn } from "@/lib/utils";

// Pill = action (Factory button.tsx). Default 32px, xs 24, sm 28, lg 36. Monochrome by default;
// `accent` is the one named electric-blue exception.
const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center rounded-full border border-transparent text-sm font-medium whitespace-nowrap select-none transition-[color,background-color,border-color,box-shadow,filter,opacity,transform] duration-fast outline-none active:translate-y-px disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "button-depth bg-primary text-primary-foreground hover:brightness-110",
        accent: "button-depth bg-brand text-brand-foreground hover:brightness-110",
        outline: "bg-background shadow-[inset_0_0_0_0.5px_var(--border-default)] hover:bg-hover",
        secondary: "button-depth bg-secondary text-secondary-foreground hover:bg-hover",
        ghost: "hover:bg-hover",
        destructive: "bg-destructive/10 text-danger-fg hover:bg-destructive/20",
        link: "text-ink-accent underline-offset-4 hover:underline px-0 h-auto",
      },
      size: {
        default: "h-8 gap-1.5 px-2.5",
        xs: "h-6 gap-1 px-2 text-xs [&_svg:not([class*='size-'])]:size-3",
        sm: "h-7 gap-1 px-2.5 text-[0.8rem] [&_svg:not([class*='size-'])]:size-3.5",
        lg: "h-9 gap-1.5 px-3",
        icon: "size-8",
        "icon-sm": "size-7 [&_svg:not([class*='size-'])]:size-3.5",
        "icon-xs": "size-6 [&_svg:not([class*='size-'])]:size-3",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  }
);

const Button = React.forwardRef(({ className, variant, size, asChild = false, type, ...props }, ref) => {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      ref={ref}
      type={asChild ? undefined : type || "button"}
      {...props}
    />
  );
});
Button.displayName = "Button";

export { Button, buttonVariants };
