import React, { useState, useEffect, useCallback } from "react";
import { getS3Health, refreshS3Health, listEndpoints, getHealthDetail, formatSize } from "../api";

function formatUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const parts = [];
  if (d > 0) parts.push(`${d}d`);
  if (h > 0) parts.push(`${h}h`);
  if (m > 0) parts.push(`${m}m`);
  parts.push(`${s}s`);
  return parts.join(" ");
}

function SystemHealthTab() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await getHealthDetail());
    } catch (e) {
      setError(e.message);
    }
    setLoading(false);
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Auto-refresh every 30s
  useEffect(() => {
    const iv = setInterval(refresh, 30000);
    return () => clearInterval(iv);
  }, [refresh]);

  if (loading && !data) return <div className="empty"><div className="spinner" /> Checking system...</div>;
  if (error && !data) return <div className="empty" style={{ color: "var(--danger)" }}>Error: {error}</div>;
  if (!data) return null;

  return (
    <div className="hc-system">
      {/* Status banner */}
      <div className={`hc-sys-banner ${data.status === "ok" ? "hc-sys-ok" : "hc-sys-degraded"}`}>
        <span className="hc-sys-dot" />
        <span>{data.status === "ok" ? "All Systems Operational" : "System Degraded"}</span>
        <span className="hc-sys-version">v{data.version}</span>
      </div>

      {/* Metrics */}
      <div className="hc-sys-metrics">
        <div className="hc-sys-metric">
          <span className="hc-sys-metric-val">{formatUptime(data.uptime_seconds)}</span>
          <span className="hc-sys-metric-lbl">Uptime</span>
        </div>
        <div className="hc-sys-metric">
          <span className="hc-sys-metric-val">{data.bucket_count}</span>
          <span className="hc-sys-metric-lbl">Buckets</span>
        </div>
        <div className="hc-sys-metric">
          <span className="hc-sys-metric-val">{data.user_count}</span>
          <span className="hc-sys-metric-lbl">Users</span>
        </div>
        <div className="hc-sys-metric">
          <span className="hc-sys-metric-val">{data.recrawl_interval}s</span>
          <span className="hc-sys-metric-lbl">Recrawl</span>
        </div>
      </div>

      {/* Service checks */}
      <table className="info-table">
        <tbody>
          <tr>
            <td className="info-label">S3 Connectivity</td>
            <td className="info-value">
              <span className={`hc-sys-dot-sm ${data.s3_connected ? "dot-ok" : "dot-err"}`} />
              {data.s3_connected ? "Connected" : "Disconnected"}
              {data.s3_error && <span style={{ color: "var(--danger)", fontSize: 11, marginLeft: 6 }}>{data.s3_error}</span>}
            </td>
          </tr>
          <tr>
            <td className="info-label">S3 Latency</td>
            <td className="info-value">
              {data.s3_latency_ms != null ? (
                <span style={data.s3_latency_ms > 500 ? { color: "var(--warning)", fontWeight: 600 } : undefined}>{data.s3_latency_ms}ms</span>
              ) : "\u2014"}
            </td>
          </tr>
          <tr><td className="info-label">S3 Endpoint</td><td className="info-value mono">{data.s3_endpoint}</td></tr>
          {data.s3_region && <tr><td className="info-label">S3 Region</td><td className="info-value">{data.s3_region}</td></tr>}
          <tr>
            <td className="info-label">Database</td>
            <td className="info-value">
              <span className={`hc-sys-dot-sm ${data.db_writable ? "dot-ok" : "dot-err"}`} />
              {data.db_writable ? "Writable" : "Read-only / Error"}
              <span className="muted" style={{ marginLeft: 6 }}>({data.db_dir})</span>
            </td>
          </tr>
          <tr><td className="info-label">Session</td><td className="info-value">{data.session_hours} hours</td></tr>
        </tbody>
      </table>

      {/* Bucket index status */}
      {data.buckets && data.buckets.length > 0 && (
        <>
          <h4 style={{ fontSize: 13, fontWeight: 600, color: "var(--text-secondary)", margin: "16px 0 8px", paddingBottom: 6, borderBottom: "1px solid var(--border-light)" }}>Bucket Index Status</h4>
          <table className="dashboard-table" style={{ fontSize: 12 }}>
            <thead>
              <tr><th>Bucket</th><th>Status</th><th>Objects</th><th>Size</th><th>Last Crawl</th></tr>
            </thead>
            <tbody>
              {data.buckets.map(b => (
                <tr key={b.name}>
                  <td className="mono">{b.name}</td>
                  <td>
                    <span className={`role-badge ${
                      b.crawling ? "role-admin" :
                      b.queued ? "role-admin" :
                      b.status === "ready" ? "role-viewer" :
                      ""
                    }`} style={b.crawling ? { animation: "pulse 1.5s ease-in-out infinite" } : undefined}>
                      {b.crawling ? "Crawling" : b.queued ? "Queued" : b.status === "ready" ? "Ready" : b.status === "not_indexed" ? "Not Indexed" : b.status}
                    </span>
                  </td>
                  <td>{b.total_objects.toLocaleString()}</td>
                  <td>{formatSize(b.total_size)}</td>
                  <td className="muted">{b.last_crawl || "\u2014"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

function S3CompatTab() {
  const [endpoints, setEndpoints] = useState([]);
  const [selectedEndpoint, setSelectedEndpoint] = useState("");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    listEndpoints().then(d => setEndpoints(d.endpoints || [])).catch(() => {});
  }, []);

  const load = async (epId) => {
    setLoading(true);
    setError("");
    try {
      setData(await getS3Health(epId || ""));
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    setError("");
    try {
      setData(await refreshS3Health(selectedEndpoint || ""));
    } catch (err) {
      setError(err.message);
    }
    setRefreshing(false);
  };

  useEffect(() => { load(selectedEndpoint); }, [selectedEndpoint]);

  const statusIcon = (status) => {
    if (status === "pass") return "\u2705";
    if (status === "unsupported") return "\u26A0\uFE0F";
    return "\u274C";
  };

  const statusClass = (status) => {
    if (status === "pass") return "hc-pass";
    if (status === "unsupported") return "hc-warn";
    return "hc-fail";
  };

  const renderEndpointResult = (epData) => (
    <div key={epData.endpoint_id} className="hc-endpoint-section">
      <div className="hc-summary">
        <div className="hc-summary-endpoint">
          <span className="hc-label">Endpoint</span>
          <span className="mono">{epData.endpoint_url || epData.endpoint || "unknown"}</span>
          {epData.endpoint_id && <span className="ep-inline-badge" style={{ marginLeft: 6 }}>{epData.endpoint_id}</span>}
        </div>
        <div className="hc-summary-score">
          <span className="hc-score">{epData.passed}/{epData.total}</span>
          <span className="hc-score-label">features supported</span>
        </div>
        {epData.tested_bucket && (
          <div className="hc-summary-bucket">
            <span className="hc-label">Tested on</span>
            <span className="mono">{epData.tested_bucket}</span>
          </div>
        )}
      </div>
      <div className="hc-grid">
        {(epData.checks || []).map((check, i) => (
          <div key={i} className={`hc-card ${statusClass(check.status)}`}>
            <div className="hc-card-icon">{statusIcon(check.status)}</div>
            <div className="hc-card-content">
              <div className="hc-card-name">{check.name}</div>
              <div className="hc-card-detail">{check.detail}</div>
            </div>
            <div className={`hc-card-badge hc-badge-${check.status}`}>
              {check.status === "pass" ? "Supported" : check.status === "unsupported" ? "Not Supported" : "Error"}
            </div>
          </div>
        ))}
      </div>
      <div className="hc-footer">
        <span className="muted" style={{ fontSize: 12 }}>
          Tested at {new Date(epData.tested_at).toLocaleString()} (cached 5 min)
        </span>
      </div>
    </div>
  );

  const isMulti = data && data.endpoints;
  const multiEndpoint = endpoints.length > 1;

  return (
    <div>
      {multiEndpoint && (
        <div style={{ marginBottom: 12 }}>
          <select
            value={selectedEndpoint}
            onChange={(e) => setSelectedEndpoint(e.target.value)}
            style={{ padding: "6px 10px", border: "1px solid var(--border-input)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text)" }}
          >
            <option value="">All Endpoints</option>
            {endpoints.map(ep => <option key={ep.id} value={ep.id}>{ep.name} ({ep.id})</option>)}
          </select>
        </div>
      )}

      {error && <div className="form-error">{error}</div>}

      {loading ? (
        <div className="empty"><div className="spinner" /> Checking endpoint{multiEndpoint ? "s" : ""}...</div>
      ) : data ? (
        <>
          {isMulti ? data.endpoints.map(ep => renderEndpointResult(ep)) : renderEndpointResult(data)}
        </>
      ) : null}

      <div style={{ marginTop: 12, textAlign: "right" }}>
        <button onClick={handleRefresh} disabled={refreshing} className="btn-primary btn-xs">
          {refreshing ? "Checking..." : "Re-check"}
        </button>
      </div>
    </div>
  );
}

export default function HealthCheck({ onClose }) {
  const [activeTab, setActiveTab] = useState("system");

  const tabs = [
    { id: "system", label: "System Health" },
    { id: "s3compat", label: "S3 Compatibility" },
  ];

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal modal-wide" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>Health Check</h2>
        <div className="tab-bar">
          {tabs.map(t => (
            <button key={t.id} className={`tab-btn ${activeTab === t.id ? "tab-active" : ""}`} onClick={() => setActiveTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>

        <div style={{ flex: 1, overflow: "auto", minHeight: 0 }}>
          {activeTab === "system" && <SystemHealthTab />}
          {activeTab === "s3compat" && <S3CompatTab />}
        </div>

        <div className="modal-actions">
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
