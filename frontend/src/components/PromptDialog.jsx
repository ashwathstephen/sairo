import React, { useState, useEffect, useRef } from "react";

export default function PromptDialog({
  title = "Input",
  message,
  placeholder = "",
  initialValue = "",
  submitLabel = "OK",
  onSubmit,
  onCancel,
}) {
  const [value, setValue] = useState(initialValue);
  const inputRef = useRef(null);
  const modalRef = useRef(null);

  useEffect(() => {
    if (inputRef.current) inputRef.current.focus();
  }, []);

  useEffect(() => {
    const el = modalRef.current;
    if (!el) return;
    const handler = (e) => {
      if (e.key === "Escape") { onCancel(); return; }
      const focusable = el.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
      if (e.key !== "Tab" || focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    el.addEventListener("keydown", handler);
    return () => el.removeEventListener("keydown", handler);
  }, [onCancel]);

  const handleSubmit = () => {
    if (!value.trim()) return;
    onSubmit(value.trim());
  };

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div
        className="modal modal-small"
        role="dialog"
        aria-modal="true"
        aria-labelledby="prompt-dialog-title"
        ref={modalRef}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="prompt-dialog-title">{title}</h2>
        {message && <p>{message}</p>}
        <input
          ref={inputRef}
          type="text"
          className="filter-input"
          style={{ width: "100%", marginBottom: 12 }}
          placeholder={placeholder}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
        />
        <div className="modal-actions">
          <button onClick={onCancel}>Cancel</button>
          <button onClick={handleSubmit} className="btn-primary" disabled={!value.trim()}>
            {submitLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
