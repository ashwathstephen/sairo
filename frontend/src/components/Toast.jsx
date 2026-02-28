import React, { useState, useCallback } from "react";

let _addToast = () => {};
let _updateToast = () => {};
let _removeToast = () => {};

export function toast(message, type = "info", duration = 4000, updateId = null, options = null) {
  const id = updateId || Date.now() + Math.random();
  const actions = options?.actions || (options?.onRetry ? [{ label: "Retry", onClick: options.onRetry }] : null);
  if (updateId) {
    _updateToast({ id: updateId, message, type, duration, actions });
  } else {
    _addToast({ message, type, id, duration, actions });
  }
  return id;
}

export default function ToastContainer() {
  const [toasts, setToasts] = useState([]);
  const timers = React.useRef({});

  const scheduleRemoval = useCallback((id, duration) => {
    if (timers.current[id]) clearTimeout(timers.current[id]);
    if (duration > 0) {
      timers.current[id] = setTimeout(() => {
        setToasts((prev) => prev.filter((x) => x.id !== id));
        delete timers.current[id];
      }, duration);
    }
  }, []);

  _addToast = useCallback((t) => {
    setToasts((prev) => [...prev, t]);
    scheduleRemoval(t.id, t.duration);
  }, [scheduleRemoval]);

  _updateToast = useCallback((t) => {
    setToasts((prev) => {
      const exists = prev.some((x) => x.id === t.id);
      if (exists) {
        return prev.map((x) => x.id === t.id ? { ...x, message: t.message, type: t.type, actions: t.actions } : x);
      }
      return [...prev, t];
    });
    if (t.duration > 0) {
      scheduleRemoval(t.id, t.duration);
    }
  }, [scheduleRemoval]);

  _removeToast = useCallback((id) => {
    setToasts((prev) => prev.filter((x) => x.id !== id));
    if (timers.current[id]) {
      clearTimeout(timers.current[id]);
      delete timers.current[id];
    }
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div className="toast-container" aria-live="polite" role="status">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.type}`}>
          <span>{t.message}</span>
          <div className="toast-buttons">
            {t.actions && t.actions.map((a, i) => (
              <button key={i} className="toast-action" onClick={() => { _removeToast(t.id); a.onClick(); }}>
                {a.label}
              </button>
            ))}
            <button className="toast-close" onClick={() => _removeToast(t.id)} aria-label="Dismiss notification">&times;</button>
          </div>
        </div>
      ))}
    </div>
  );
}
