import * as React from "react";
import { cn } from "@/lib/utils";
import { Icon } from "@/components/Icon";

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  className,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: React.ReactNode;
  className?: string;
}) {
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    // The dialog is never taller than the screen: its header stays put and its
    // body scrolls. Centring a dialog that is taller than the viewport instead
    // pushes its top edge off-screen, where no amount of scrolling reaches it,
    // which is what a sale with more than three line items used to do.
    <div className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-4">
      <div
        className="fixed inset-0 bg-black/50 backdrop-blur-[2px] animate-in"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={cn(
          "relative z-10 flex w-full max-w-lg flex-col rounded-2xl border border-border bg-card shadow-xl",
          // dvh follows a phone browser's collapsing toolbar; vh is the fallback.
          "max-h-[calc(100vh-1.5rem)] supports-[height:100dvh]:max-h-[calc(100dvh-1.5rem)]",
          className,
        )}
      >
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-5 py-4">
          <div>
            <h2 className="text-lg font-bold tracking-[-0.03em]">{title}</h2>
            {description && <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="-mr-1 inline-flex h-9 w-9 items-center justify-center rounded-xl text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <Icon name="x" size={18} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-4">
          {children}
        </div>
      </div>
    </div>
  );
}
