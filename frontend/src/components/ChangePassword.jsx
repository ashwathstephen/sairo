import React, { useState } from "react";
import { changePassword } from "../api";

export default function ChangePassword({ onClose }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const [loading, setLoading] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    if (next.length < 8) return setError("New password must be at least 8 characters");
    if (next !== confirm) return setError("New passwords do not match");
    setLoading(true);
    try {
      await changePassword(current, next);
      setDone(true);
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  };

  // Which field the message is about, so the border marks the right one and the input carries the
  // description for a screen reader rather than the message floating unattached above the form.
  const mismatch = error === "New passwords do not match";
  const tooShort = error.startsWith("New password must be");

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal modal-small" role="dialog" aria-modal="true" aria-labelledby="cp-title"
           onClick={(e) => e.stopPropagation()}>
        <h2 id="cp-title">Change Password</h2>
        {done ? (
          <>
            <p className="form-hint" style={{ margin: "0 0 16px" }}>Your password has been updated.</p>
            <div className="modal-actions">
              <button onClick={onClose} className="btn-primary" autoFocus>Done</button>
            </div>
          </>
        ) : (
          <form onSubmit={submit}>
            {error && <div className="form-error" role="alert">{error}</div>}
            <div className="form-field">
              <label htmlFor="cp-current">Current password</label>
              <input id="cp-current" type="password" value={current} autoFocus
                     onChange={(e) => setCurrent(e.target.value)} required autoComplete="current-password" />
            </div>
            <div className="form-field">
              <label htmlFor="cp-new">New password</label>
              <input id="cp-new" type="password" value={next} minLength={8} required
                     aria-invalid={tooShort || undefined} aria-describedby="cp-new-hint"
                     onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
            </div>
            <p className="form-hint" id="cp-new-hint">At least 8 characters.</p>
            <div className="form-field">
              <label htmlFor="cp-confirm">Confirm new password</label>
              <input id="cp-confirm" type="password" value={confirm} required
                     aria-invalid={mismatch || undefined}
                     onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
            </div>
            <div className="modal-actions">
              <button type="button" onClick={onClose}>Cancel</button>
              <button type="submit" disabled={loading} className="btn-primary">
                {loading ? "Updating…" : "Update Password"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
