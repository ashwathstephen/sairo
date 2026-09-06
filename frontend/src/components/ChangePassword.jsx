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

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>Change Password</h2>
        {done ? (
          <>
            <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: "0 0 16px" }}>Your password has been updated.</p>
            <div className="modal-actions"><button onClick={onClose} className="btn-primary">Done</button></div>
          </>
        ) : (
          <form onSubmit={submit}>
            {error && <div className="form-error">{error}</div>}
            <input type="password" placeholder="Current password" aria-label="Current password" value={current} onChange={(e) => setCurrent(e.target.value)} required autoComplete="current-password" />
            <input type="password" placeholder="New password (8+ characters)" aria-label="New password" value={next} onChange={(e) => setNext(e.target.value)} required minLength={8} autoComplete="new-password" />
            <input type="password" placeholder="Confirm new password" aria-label="Confirm new password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required autoComplete="new-password" />
            <div className="modal-actions">
              <button type="submit" disabled={loading} className="btn-primary">{loading ? "Updating..." : "Update Password"}</button>
              <button type="button" onClick={onClose}>Cancel</button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
