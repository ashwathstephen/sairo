import React, { useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { setup2FA, enable2FA, disable2FA } from "../api";

export default function TwoFactorSetup({ onClose, totpEnabled, onStatusChange }) {
  const [step, setStep] = useState(totpEnabled ? "status" : "start"); // start | setup | verify | recovery | status | disabling
  const [secret, setSecret] = useState("");
  const [otpauthUrl, setOtpauthUrl] = useState("");
  const [code, setCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [disablePassword, setDisablePassword] = useState("");

  const handleSetup = async () => {
    setLoading(true);
    setError("");
    try {
      const data = await setup2FA();
      setSecret(data.secret);
      setOtpauthUrl(data.otpauth_url);
      setStep("setup");
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  };

  const handleVerify = async () => {
    if (code.length !== 6) return;
    setLoading(true);
    setError("");
    try {
      const data = await enable2FA(code);
      setRecoveryCodes(data.recovery_codes || []);
      setStep("recovery");
      if (onStatusChange) onStatusChange(true);
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  };

  const handleDisable = async () => {
    setLoading(true);
    setError("");
    try {
      await disable2FA(disablePassword);
      if (onStatusChange) onStatusChange(false);
      onClose();
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  };

  const copyRecoveryCodes = () => {
    navigator.clipboard.writeText(recoveryCodes.join("\n")).catch(() => {});
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>Two-Factor Authentication</h2>

        {error && <div className="form-error">{error}</div>}

        {step === "start" && (
          <>
            <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: "0 0 16px" }}>
              Add an extra layer of security to your account. You'll need an authenticator app like
              Google Authenticator, Authy, or 1Password.
            </p>
            <div className="modal-actions">
              <button onClick={handleSetup} disabled={loading} className="btn-primary">
                {loading ? "Setting up..." : "Set Up 2FA"}
              </button>
              <button onClick={onClose}>Cancel</button>
            </div>
          </>
        )}

        {step === "setup" && (
          <>
            <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: "0 0 12px" }}>
              Scan this QR code with your authenticator app, or enter the key manually:
            </p>
            <div className="tfa-secret-box">
              <div className="tfa-qr-wrap">
                <QRCodeSVG value={otpauthUrl} size={180} level="M" includeMargin />
              </div>
              <div className="tfa-manual">
                <span className="tfa-label">Or enter this key manually:</span>
                <code className="tfa-key">{secret}</code>
              </div>
            </div>
            <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: "12px 0 8px" }}>
              Enter the 6-digit code from your authenticator app:
            </p>
            <div className="tfa-code-input">
              <input
                type="text"
                inputMode="numeric"
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                placeholder="000000"
                autoFocus
                className="tfa-code"
                onKeyDown={(e) => e.key === "Enter" && handleVerify()}
              />
            </div>
            <div className="modal-actions">
              <button onClick={handleVerify} disabled={code.length !== 6 || loading} className="btn-primary">
                {loading ? "Verifying..." : "Verify & Enable"}
              </button>
              <button onClick={() => { setStep("start"); setError(""); }}>Back</button>
            </div>
          </>
        )}

        {step === "recovery" && (
          <>
            <div className="tfa-recovery-banner">
              Save these recovery codes in a safe place. Each code can only be used once.
              If you lose access to your authenticator app, you can use these to sign in.
            </div>
            <div className="tfa-recovery-grid">
              {recoveryCodes.map((c, i) => (
                <code key={i} className="tfa-recovery-code">{c}</code>
              ))}
            </div>
            <div className="modal-actions">
              <button onClick={copyRecoveryCodes} className="btn-primary">Copy Codes</button>
              <button onClick={onClose}>I've Saved These</button>
            </div>
          </>
        )}

        {step === "status" && (
          <>
            <div className="tfa-status-enabled">
              <span className="tfa-status-icon">&#9989;</span>
              <span>Two-factor authentication is <strong>enabled</strong>.</span>
            </div>
            <div className="modal-actions">
              <button onClick={() => { setStep("disabling"); setError(""); }} className="btn-danger">
                Disable 2FA
              </button>
              <button onClick={onClose}>Close</button>
            </div>
          </>
        )}

        {step === "disabling" && (
          <>
            <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: "0 0 12px" }}>
              Enter your password to confirm disabling 2FA:
            </p>
            <input
              type="password"
              value={disablePassword}
              onChange={(e) => setDisablePassword(e.target.value)}
              placeholder="Password"
              autoFocus
              style={{ width: "100%", marginBottom: 12, padding: "8px 12px", border: "1px solid var(--border-input)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text)" }}
              onKeyDown={(e) => e.key === "Enter" && handleDisable()}
            />
            <div className="modal-actions">
              <button onClick={handleDisable} disabled={!disablePassword || loading} className="btn-danger">
                {loading ? "Disabling..." : "Confirm Disable"}
              </button>
              <button onClick={() => { setStep("status"); setError(""); }}>Cancel</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
