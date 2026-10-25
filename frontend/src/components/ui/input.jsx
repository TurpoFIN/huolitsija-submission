import * as React from "react";
import { cn } from "@/lib/utils";

// 32px high, 10px horizontal padding, 6px radius; 16px text below 768px (no iOS zoom), 13px on desktop.
const Input = React.forwardRef(({ className, type, ...props }, ref) => (
  <input
    type={type}
    data-slot="input"
    className={cn(
      "surface-input flex h-8 w-full min-w-0 rounded-lg bg-transparent px-2.5 py-1 text-md md:text-sm transition-[box-shadow] placeholder:text-ink-tertiary file:border-0 file:bg-transparent file:text-sm file:font-medium disabled:cursor-not-allowed disabled:opacity-50",
      className
    )}
    ref={ref}
    {...props}
  />
));
Input.displayName = "Input";

export { Input };
