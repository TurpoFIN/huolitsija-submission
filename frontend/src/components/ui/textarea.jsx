import * as React from "react";
import { cn } from "@/lib/utils";

const Textarea = React.forwardRef(({ className, ...props }, ref) => (
  <textarea
    data-slot="textarea"
    className={cn(
      "surface-input flex min-h-[72px] w-full rounded-lg bg-transparent px-2.5 py-2 text-md md:text-sm placeholder:text-ink-tertiary disabled:cursor-not-allowed disabled:opacity-50",
      className
    )}
    ref={ref}
    {...props}
  />
));
Textarea.displayName = "Textarea";

export { Textarea };
