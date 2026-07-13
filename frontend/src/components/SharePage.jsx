import React, { useState, useEffect } from "react";
import { resolveShareLink, getBranding } from "../api";

export default function SharePage({ token }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [password, setPassword] = useState("");
  const [needsPassword, setNeedsPassword] = useState(false);
  const [brand, setBrand] = useState({ app_name: "Sairo" });

  // Brand the public share page (name + accent) from the deployment's settings.
  useEffect(() => { getBranding().then(setBrand).catch(() => {}); }, []);

  const fetchLink = async (pwd = "") => {
    setLoading(true);
    setError(null);
    try {
      const result = await resolveShareLink(token, pwd);
      setData(result);
      setLoading(false);
    } catch (e) {
      if (e.message.includes("Password required")) {
        setNeedsPassword(true);
        setLoading(false);
      } else {
        setError(e.message);
        setLoading(false);
      }
    }
  };

  useEffect(() => { fetchLink(); }, [token]);

  const handleDownload = () => {
    if (data?.url) window.open(data.url, "_blank");
  };

  const handlePasswordSubmit = (e) => {
    e.preventDefault();
    fetchLink(password);
  };

  return (
    <div className="share-page">
      <div className="share-card">
        <div className="share-logo">
          {brand.app_logo ? (
            <img src={brand.app_logo} alt={brand.app_name || "Sairo"} style={{ height: 28, width: "auto" }} />
          ) : (
            <svg viewBox="0 0 40 40" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" width="28" height="28" style={{ color: brand.primary_color || "#3b82f6" }}>
              <path d="M28 10c0 3-4 5.5-8 5.5S12 13 12 10s4-5.5 8-5.5 8 2.5 8 5.5z"/><path d="M28 20c0 3-4 5.5-8 5.5S12 23 12 20"/><path d="M28 30c0 3-4 5.5-8 5.5S12 33 12 30"/><line x1="12" y1="10" x2="12" y2="30"/><line x1="28" y1="10" x2="28" y2="30"/>
            </svg>
          )}
          {brand.app_name || "Sairo"}
        </div>
        {loading ? (
          <div className="share-loading"><div className="spinner" /> Loading...</div>
        ) : error ? (
          <div className="share-error">{error}</div>
        ) : needsPassword && !data ? (
          <form onSubmit={handlePasswordSubmit} className="share-password-form">
            <p>This file is password protected.</p>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Enter password"
              autoFocus
            />
            <button type="submit" className="btn-primary">Access File</button>
          </form>
        ) : data ? (
          <div className="share-content">
            <div className="share-filename">{data.filename}</div>
            <div className="share-meta">
              <span>{data.bucket}</span> / <span>{data.key}</span>
            </div>
            <button onClick={handleDownload} className="btn-primary share-download-btn">
              Download File
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
