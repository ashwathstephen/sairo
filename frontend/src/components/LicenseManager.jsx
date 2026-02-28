import React, { useState, useEffect } from "react";
import { getLicense, activateLicense } from "../api";

export default function LicenseManager({ onClose }) {
  const [license, setLicense] = useState(null);
  const [loading, setLoading] = useState(true);
  const [key, setKey] = useState("");
  const [activating, setActivating] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const data = await getLicense();
      setLicense(data);
    } catch {
      setLicense(null);
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  const handleActivate = async () => {
    if (!key.trim()) return;
    setActivating(true);
    setError("");
    setSuccess("");
    try {
      const result = await activateLicense(key.trim());
      if (result.ok) {
        setSuccess("License activated successfully!");
        setKey("");
        load();
      } else {
        setError(result.error || "Invalid license key");
      }
    } catch (e) {
      setError(e.message);
    }
    setActivating(false);
  };

  const isActive = license && license.license_type && license.license_type !== "community";
  const isExpired = license?.expires_at && new Date(license.expires_at) < new Date();

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>License</h2>

        {loading ? (
          <div className="empty"><div className="spinner" /> Loading...</div>
        ) : (
          <>
            <div className="license-status">
              <div className="license-badge-row">
                <span className={`license-badge ${isActive && !isExpired ? "license-pro" : "license-community"}`}>
                  {isActive && !isExpired ? license.license_type.charAt(0).toUpperCase() + license.license_type.slice(1) : "Community"}
                </span>
                {isExpired && <span className="license-badge license-expired">Expired</span>}
              </div>
              {isActive && (
                <table className="info-table" style={{ marginTop: 12 }}>
                  <tbody>
                    <tr><td className="info-label">Licensed To</td><td className="info-value">{license.licensed_to || "—"}</td></tr>
                    <tr><td className="info-label">Max Users</td><td className="info-value">{license.max_users || "Unlimited"}</td></tr>
                    {license.expires_at && <tr><td className="info-label">Expires</td><td className="info-value">{new Date(license.expires_at).toLocaleDateString()}</td></tr>}
                    {license.activated_at && <tr><td className="info-label">Activated</td><td className="info-value">{new Date(license.activated_at).toLocaleDateString()}</td></tr>}
                  </tbody>
                </table>
              )}
              {!isActive && (
                <p className="muted" style={{ marginTop: 8, fontSize: 13 }}>
                  Running on the free Community edition. Enter a license key to unlock Pro features.
                </p>
              )}
            </div>

            <div style={{ marginTop: 16 }}>
              <label style={{ fontSize: 13, fontWeight: 600, display: "block", marginBottom: 6 }}>
                {isActive ? "Update License Key" : "Activate License"}
              </label>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  type="text"
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  placeholder="Paste your license key"
                  className="token-input"
                  style={{ flex: 1 }}
                />
                <button onClick={handleActivate} disabled={activating || !key.trim()} className="btn-primary">
                  {activating ? "Activating..." : "Activate"}
                </button>
              </div>
              {error && <p style={{ color: "var(--danger)", fontSize: 13, marginTop: 6 }}>{error}</p>}
              {success && <p style={{ color: "var(--success)", fontSize: 13, marginTop: 6 }}>{success}</p>}
            </div>
          </>
        )}

        <div className="modal-actions">
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
