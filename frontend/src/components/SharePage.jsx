import React, { useState, useEffect } from "react";
import { resolveShareLink } from "../api";

export default function SharePage({ token }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [password, setPassword] = useState("");
  const [needsPassword, setNeedsPassword] = useState(false);

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
        <div className="share-logo">Sairo</div>
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
