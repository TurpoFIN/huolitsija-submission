import React, { useRef, useState } from "react";
import { Paperclip, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/** Labelled upload button + drop zone; shows the selected filename. onFiles(File[]) */
export function FileUpload({ onFiles, accept = ".pdf,.png,.jpg,.jpeg,.webp,.xlsx,.csv", multiple = false, label, busy, className, hint }) {
  const { t } = useT();
  const ref = useRef(null);
  const [names, setNames] = useState([]);
  const [over, setOver] = useState(false);
  const handle = (files) => {
    const list = Array.from(files || []);
    if (!list.length) return;
    setNames(list.map((f) => f.name));
    onFiles(list);
  };
  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); handle(e.dataTransfer.files); }}
      className={cn("flex flex-wrap items-center gap-3 rounded-xl border border-dashed px-4 py-3", over && "bg-hover", className)}
    >
      <input ref={ref} type="file" className="sr-only" accept={accept} multiple={multiple} onChange={(e) => { handle(e.target.files); e.target.value = ""; }}
        aria-label={label || t("action.choose_file")} />
      <Button variant="outline" size="sm" onClick={() => ref.current?.click()} disabled={busy}>
        <Upload /> {label || t("action.choose_file")}
      </Button>
      <span className="min-w-0 flex-1 truncate text-sm text-ink-tertiary">
        {busy ? t("state.saving") : names.length ? (<span className="inline-flex items-center gap-1 text-foreground"><Paperclip className="size-3.5" />{names.join(", ")}</span>) : hint}
      </span>
    </div>
  );
}
