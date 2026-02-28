import React, { useEffect, useRef } from "react";

export default function ConfirmDialog({
  title = "Confirm",
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "info", // "danger" | "info"
  hideCancel = false,
  onConfirm,
  onCancel,
}) {
  const modalRef = useRef(null);

  useEffect(() => {
    const el = modalRef.current;
    if (!el) return;
    const focusable = el.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    if (focusable.length > 0) focusable[0].focus();

    const handler = (e) => {
      if (e.key === "Escape") { onCancel(); return; }
      if (e.key !== "Tab" || focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    el.addEventListener("keydown", handler);
    return () => el.removeEventListener("keydown", handler);
  }, [onCancel]);

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div
        className="modal modal-small"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        ref={modalRef}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="confirm-dialog-title">{title}</h2>
        {typeof message === "string" ? <p>{message}</p> : message}
        <div className="modal-actions">
          {!hideCancel && <button onClick={onCancel}>{cancelLabel}</button>}
          <button
            onClick={onConfirm || onCancel}
            className={variant === "danger" ? "btn-danger" : "btn-primary"}
          >
            {hideCancel ? "OK" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
