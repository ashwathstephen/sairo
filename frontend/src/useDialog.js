import { useEffect, useRef } from "react";

const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

/**
 * Keyboard behaviour every modal dialog owes its user: Escape closes it, Tab cycles inside it
 * instead of walking out into the page behind, and focus goes back to whatever opened it.
 *
 * Extracted from ConfirmDialog, which was one of only three dialogs of nineteen that did this.
 * The rest — Change Password included — let focus escape into background controls that a sighted
 * mouse user cannot even see are focused.
 *
 * Returns a ref to put on the dialog element.
 */
export function useDialogKeys(onClose) {
  const ref = useRef(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const opener = document.activeElement;   // restore this when the dialog goes away
    const focusable = () => Array.from(el.querySelectorAll(FOCUSABLE)).filter((n) => !n.disabled);

    // Focus the first control here rather than with autoFocus on the element: React applies
    // autoFocus during commit, before this effect runs, so `opener` above would capture the
    // dialog's own input and the restore on close would have nothing outside to go back to.
    const initial = focusable();
    if (initial.length) initial[0].focus();

    const handler = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); return; }
      if (e.key !== "Tab") return;
      // Recomputed per keypress: a dialog's contents change (a success screen swaps every control).
      const items = focusable();
      if (items.length === 0) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    el.addEventListener("keydown", handler);
    return () => {
      el.removeEventListener("keydown", handler);
      if (opener && typeof opener.focus === "function" && document.contains(opener)) opener.focus();
    };
  }, [onClose]);

  return ref;
}
