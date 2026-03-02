import React, { useState, useEffect } from "react";
import { login, loginLdap } from "../auth";
import { verify2FA, recover2FA } from "../api";

const FEATURES = [
  { icon: "\uD83D\uDD0D", title: "Full-text Search", desc: "Find any file across all your buckets instantly" },
  { icon: "\uD83D\uDCC4", title: "File Preview", desc: "Preview images, PDFs, text, CSV, JSON, and Parquet schemas" },
  { icon: "\u2328\uFE0F", title: "Keyboard Shortcuts", desc: "Navigate fast with /, Backspace, Esc, and ? shortcuts" },
  { icon: "\uD83D\uDDC2\uFE0F", title: "Version Control", desc: "Browse, restore, and manage object versions" },
];

export default function Login({ onLogin, branding = {} }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [authMode, setAuthMode] = useState("local"); // "local" or "ldap"
  // 2FA state
  const [needs2FA, setNeeds2FA] = useState(false);
  const [tfaCode, setTfaCode] = useState("");
  const [useRecovery, setUseRecovery] = useState(false);

  const ldapEnabled = branding.ldap_enabled;
  const appName = branding.app_name || "Sairo";
  const loginMessage = branding.login_message;
  const oauthProviders = branding.oauth_providers || [];

  // Check URL params for OAuth 2FA redirect
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("requires_2fa") === "true") {
      setNeeds2FA(true);
      setUsername("(OAuth)");
      window.history.replaceState({}, "", "/");
    }
  }, []);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (authMode === "ldap") {
        await loginLdap(username, password);
      } else {
        await login(username, password);
      }
      onLogin();
    } catch (err) {
      if (err.requires_2fa) {
        setNeeds2FA(true);
        setError("");
      } else {
        setError(err.message);
      }
    } finally {
      setLoading(false);
    }
  };

  const handle2FASubmit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (useRecovery) {
        await recover2FA(tfaCode.trim());
      } else {
        await verify2FA(tfaCode.trim());
      }
      onLogin();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // 2FA verification form
  if (needs2FA) {
    return (
      <div className="login-page">
        <div className="login-container">
          <form className="login-form" onSubmit={handle2FASubmit}>
            <div className="login-branding">
              <div className="login-logo-mark">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="28" height="28">
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                  <circle cx="12" cy="16" r="1"/>
                </svg>
              </div>
              <h1>Two-Factor Authentication</h1>
            </div>
            <p className="login-subtitle">
              {useRecovery ? "Enter a recovery code" : "Enter the 6-digit code from your authenticator app"}
            </p>
            {error && <div className="login-error">{error}</div>}
            <input
              type="text"
              inputMode={useRecovery ? "text" : "numeric"}
              maxLength={useRecovery ? 20 : 6}
              value={tfaCode}
              onChange={(e) => setTfaCode(useRecovery ? e.target.value : e.target.value.replace(/\D/g, "").slice(0, 6))}
              placeholder={useRecovery ? "Recovery code" : "000000"}
              autoFocus
              required
              className="tfa-login-input"
              aria-label={useRecovery ? "Recovery code" : "Authentication code"}
            />
            <button type="submit" disabled={loading || (!useRecovery && tfaCode.length !== 6) || (useRecovery && !tfaCode.trim())} className="btn-primary">
              {loading ? "Verifying..." : "Verify"}
            </button>
            <button
              type="button"
              onClick={() => { setUseRecovery(!useRecovery); setTfaCode(""); setError(""); }}
              style={{ background: "none", border: "none", color: "var(--primary)", cursor: "pointer", fontSize: 13, marginTop: 4 }}
            >
              {useRecovery ? "Use authenticator code instead" : "Use a recovery code"}
            </button>
          </form>
          <div className="login-features">
            {FEATURES.map((f, i) => (
              <div key={i} className="login-feature">
                <span className="login-feature-icon">{f.icon}</span>
                <div>
                  <strong>{f.title}</strong>
                  <p>{f.desc}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="login-page">
      <div className="login-container">
        <form className="login-form" onSubmit={handleSubmit}>
          <div className="login-branding">
            <div className="login-logo-mark">
              <svg viewBox="0 0 40 40" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" width="32" height="32">
                <path d="M28 10c0 3-4 5.5-8 5.5S12 13 12 10s4-5.5 8-5.5 8 2.5 8 5.5z"/>
                <path d="M28 20c0 3-4 5.5-8 5.5S12 23 12 20"/>
                <path d="M28 30c0 3-4 5.5-8 5.5S12 33 12 30"/>
                <line x1="12" y1="10" x2="12" y2="30"/>
                <line x1="28" y1="10" x2="28" y2="30"/>
              </svg>
            </div>
            <h1>{appName}</h1>
            <p className="login-subtitle">Object storage, <span className="login-subtitle-fade">beautifully browsed.</span></p>
          </div>
          {loginMessage && <p className="login-message">{loginMessage}</p>}
          {error && <div className="login-error">{error}</div>}
          {ldapEnabled && (
            <div className="login-auth-toggle">
              <button type="button" className={`auth-toggle-btn ${authMode === "local" ? "auth-toggle-active" : ""}`} onClick={() => setAuthMode("local")}>Local</button>
              <button type="button" className={`auth-toggle-btn ${authMode === "ldap" ? "auth-toggle-active" : ""}`} onClick={() => setAuthMode("ldap")}>LDAP</button>
            </div>
          )}
          <input
            type="text"
            placeholder="Username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            required
            aria-label="Username"
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            aria-label="Password"
          />
          <button type="submit" disabled={loading} className="btn-primary">
            {loading ? "Signing in..." : authMode === "ldap" ? "Sign In with LDAP" : "Sign In"}
          </button>
          {oauthProviders.length > 0 && (
            <div className="login-oauth-divider">
              <span>or continue with</span>
            </div>
          )}
          {oauthProviders.length > 0 && (
            <div className="login-oauth-buttons">
              {oauthProviders.map(p => (
                <a key={p.id} href={`/api/auth/oauth/${p.id}/login`} className="btn-oauth">
                  {p.id === "google" && <svg viewBox="0 0 24 24" width="18" height="18"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>}
                  {p.id === "github" && <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M12 2C6.477 2 2 6.477 2 12c0 4.42 2.865 8.17 6.839 9.49.5.092.682-.217.682-.482 0-.237-.008-.866-.013-1.7-2.782.604-3.369-1.34-3.369-1.34-.454-1.156-1.11-1.464-1.11-1.464-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.564 9.564 0 0112 6.844a9.59 9.59 0 012.504.337c1.909-1.294 2.747-1.025 2.747-1.025.546 1.377.203 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.167 22 16.418 22 12c0-5.523-4.477-10-10-10z"/></svg>}
                  Sign in with {p.name}
                </a>
              ))}
            </div>
          )}
        </form>
        <div className="login-features">
          {FEATURES.map((f, i) => (
            <div key={i} className="login-feature">
              <span className="login-feature-icon">{f.icon}</span>
              <div>
                <strong>{f.title}</strong>
                <p>{f.desc}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
