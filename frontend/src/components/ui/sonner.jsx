import { Toaster as Sonner, toast } from "sonner"
import { useT } from "@/lib/i18n"

const Toaster = ({
  ...props
}) => {
  const { t } = useT()
  const theme = document.documentElement.getAttribute("data-theme") || "light"

  return (
    <Sonner
      theme={theme}
      containerAriaLabel={t("review.notifications")}
      className="toaster group"
      toastOptions={{
        classNames: {
          toast:
            "group toast group-[.toaster]:bg-popover group-[.toaster]:text-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg group-[.toaster]:rounded-xl",
          description: "group-[.toast]:text-muted-foreground",
          actionButton:
            "group-[.toast]:bg-primary group-[.toast]:text-primary-foreground",
          cancelButton:
            "group-[.toast]:bg-muted group-[.toast]:text-muted-foreground",
        },
      }}
      {...props} />
  );
}

export { Toaster, toast }
