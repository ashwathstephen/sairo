import React, { useState, useEffect } from "react";
import { getStorageBreakdown, getStorageHistory, getCostBreakdown, getOptimizationSummary, formatSize } from "../api";

function formatPrecise(bytes, refMin, refMax) {
  if (bytes === 0 && (!refMax || refMax === 0)) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const ref = Math.abs(refMax || bytes);
  if (ref === 0) return "0 B";
  const i = Math.min(Math.floor(Math.log(ref) / Math.log(1024)), units.length - 1);
  const val = bytes / Math.pow(1024, i);
  const rangeInUnit = (refMax - refMin) / Math.pow(1024, i);
  let decimals = 1;
  if (rangeInUnit < 0.5) decimals = 3;
  else if (rangeInUnit < 5) decimals = 2;
  return val.toFixed(decimals) + " " + units[i];
}

function TrendChart({ history, label, valueKey = "total_size", formatter = formatSize }) {
  const [hovered, setHovered] = useState(null);

  if (!history || history.length < 2) {
    return <p className="muted" style={{ padding: "8px 0", fontSize: 13 }}>Not enough data yet. Trends appear after multiple crawls.</p>;
  }

  const values = history.map(h => h[valueKey]);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const isFlat = rawMin === rawMax;
  const rawRange = isFlat ? (rawMin * 0.04 || 1) : (rawMax - rawMin);
  const min = rawMin - rawRange * 0.1;
  const max = rawMax + rawRange * 0.1;
  const range = max - min;

  const W = 600, H = 160, PAD_X = 70, PAD_Y = 20, PAD_B = 30;
  const chartW = W - PAD_X - 10;
  const chartH = H - PAD_Y - PAD_B;

  const points = history.map((h, i) => ({
    x: PAD_X + (i / (history.length - 1)) * chartW,
    y: PAD_Y + chartH - ((h[valueKey] - min) / range) * chartH,
    val: h[valueKey],
    ts: h.timestamp,
    count: h.object_count,
  }));

  const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ");
  const areaPath = linePath + ` L${points[points.length - 1].x},${PAD_Y + chartH} L${points[0].x},${PAD_Y + chartH} Z`;

  const first = values[0];
  const last = values[values.length - 1];
  const delta = last - first;
  const deltaPct = first > 0 ? ((delta / first) * 100).toFixed(1) : "0";
  const deltaColor = delta > 0 ? "var(--success, #22c55e)" : delta < 0 ? "var(--danger, #ef4444)" : "var(--text-muted)";
  const deltaSign = delta > 0 ? "+" : "";

  const yTicks = Array.from({ length: 5 }, (_, i) => {
    const val = min + (range * i) / 4;
    const y = PAD_Y + chartH - (i / 4) * chartH;
    return { val, y, label: formatPrecise(val, min, max) };
  });

  const firstDate = new Date(history[0].timestamp).toDateString();
  const lastDate = new Date(history[history.length - 1].timestamp).toDateString();
  const sameDay = firstDate === lastDate;

  const maxLabels = Math.min(5, history.length);
  const xLabelIndices = Array.from({ length: maxLabels }, (_, i) =>
    Math.round(i * (history.length - 1) / (maxLabels - 1))
  ).filter((v, i, a) => a.indexOf(v) === i);

  const xLabels = xLabelIndices.map(i => ({
    x: points[i].x,
    label: sameDay
      ? new Date(history[i].timestamp).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
      : new Date(history[i].timestamp).toLocaleDateString(undefined, { month: "short", day: "numeric" }),
  }));

  return (
    <div className="trend-chart-container">
      <div className="trend-chart-header">
        <span className="trend-chart-label">{label}</span>
        <span className="trend-chart-delta" style={{ color: deltaColor }}>
          {deltaSign}{formatter(Math.abs(delta))} ({deltaSign}{deltaPct}%)
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="trend-chart-svg" preserveAspectRatio="xMidYMid meet"
        onMouseLeave={() => setHovered(null)}>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={PAD_X} y1={t.y} x2={W - 10} y2={t.y} stroke="var(--border)" strokeWidth="0.5" strokeDasharray="4,4" />
            <text x={PAD_X - 6} y={t.y + 4} textAnchor="end" fill="var(--text-muted)" fontSize="9">{t.label}</text>
          </g>
        ))}
        {xLabels.map((l, i) => (
          <text key={i} x={l.x} y={H - 5} textAnchor="middle" fill="var(--text-muted)" fontSize="9">{l.label}</text>
        ))}
        <path d={areaPath} fill="rgba(59,130,246,0.1)" />
        <path d={linePath} fill="none" stroke="#3b82f6" strokeWidth="2" strokeLinejoin="round" />
        {hovered !== null && (
          <line x1={points[hovered].x} y1={PAD_Y} x2={points[hovered].x} y2={PAD_Y + chartH}
            stroke="var(--text-muted)" strokeWidth="0.5" strokeDasharray="3,3" />
        )}
        {points.map((p, i) => (
          <circle key={i} cx={p.x} cy={p.y} r={hovered === i ? 5 : 3}
            fill="#3b82f6" stroke="var(--bg-card, #1e1e2e)" strokeWidth="1.5"
            onMouseEnter={() => setHovered(i)} style={{ cursor: "pointer" }} />
        ))}
        {hovered !== null && (() => {
          const p = points[hovered];
          const ts = new Date(p.ts);
          const timeStr = sameDay
            ? ts.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })
            : ts.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
              ts.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
          const text = `${timeStr}: ${formatPrecise(p.val, rawMin, rawMax)} (${p.count.toLocaleString()} objects)`;
          const tooltipX = p.x < W / 2 ? p.x + 8 : p.x - 8;
          const anchor = p.x < W / 2 ? "start" : "end";
          return (
            <g>
              <rect x={anchor === "start" ? tooltipX - 2 : tooltipX - text.length * 4.5 - 4}
                y={p.y - 22} width={text.length * 4.5 + 8} height={16}
                rx="3" fill="#1e293b" stroke="#475569" strokeWidth="0.5" opacity="0.95" />
              <text x={tooltipX} y={p.y - 10} textAnchor={anchor}
                fill="#f1f5f9" fontSize="9" fontWeight="500">{text}</text>
            </g>
          );
        })()}
      </svg>
    </div>
  );
}

const PROVIDER_LABELS = {
  aws: "AWS S3", r2: "Cloudflare R2", b2: "Backblaze B2", wasabi: "Wasabi",
  leaseweb: "Leaseweb", digitalocean: "DigitalOcean", hetzner: "Hetzner",
  scaleway: "Scaleway", ovh: "OVHcloud", idrive_e2: "iDrive e2", storj: "Storj",
  minio: "MinIO (self-hosted)", ceph: "Ceph (self-hosted)", unknown: "Unknown",
};

function formatCost(n) {
  if (n == null) return "--";
  if (n >= 1000) return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
  if (n >= 1) return "$" + n.toFixed(2);
  if (n > 0) return "<$0.01";
  return "$0";
}

const SEVERITY_STYLES = {
  high: { bg: "rgba(239, 68, 68, 0.08)", border: "rgba(239, 68, 68, 0.25)", color: "#ef4444", label: "High" },
  medium: { bg: "rgba(245, 158, 11, 0.08)", border: "rgba(245, 158, 11, 0.25)", color: "#f59e0b", label: "Medium" },
  low: { bg: "rgba(59, 130, 246, 0.08)", border: "rgba(59, 130, 246, 0.25)", color: "#3b82f6", label: "Info" },
};

const TAB_STYLE = (active) => ({
  padding: "6px 16px", fontSize: 13, fontWeight: active ? 600 : 400, cursor: "pointer",
  background: active ? "rgba(59, 130, 246, 0.15)" : "transparent",
  color: active ? "#3b82f6" : "var(--text-muted)",
  border: "none", borderRadius: 6, transition: "all 0.15s",
});

export default function StorageDashboard({ bucket, onClose, onNavigate }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [history, setHistory] = useState(null);
  const [folderHistory, setFolderHistory] = useState({});
  const [expandedFolder, setExpandedFolder] = useState(null);

  const [costData, setCostData] = useState(null);
  const [costLoading, setCostLoading] = useState(false);
  const [selectedProvider, setSelectedProvider] = useState("");

  const [activeTab, setActiveTab] = useState("storage");
  const [optData, setOptData] = useState(null);
  const [optLoading, setOptLoading] = useState(false);
  const [optError, setOptError] = useState(null);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      getStorageBreakdown(bucket, ""),
      getStorageHistory(bucket, "", 90),
    ])
      .then(([d, h]) => {
        setData(d);
        setHistory(h.history || []);
        setLoading(false);
        getCostBreakdown(bucket).then(c => {
          setCostData(c);
          setSelectedProvider(c.provider);
        }).catch(() => { /* cost is optional — silent fallback */ });
      })
      .catch(e => { setError(e.message); setLoading(false); });
  }, [bucket]);

  // Lazy-load optimization data when tab is clicked
  useEffect(() => {
    if (activeTab === "optimize" && !optData && !optLoading) {
      setOptLoading(true);
      setOptError(null);
      getOptimizationSummary(bucket).then(d => {
        setOptData(d);
        setOptLoading(false);
        setOptError(null);
      }).catch(e => {
        setOptLoading(false);
        setOptError(e.message || "Failed to load optimization data");
      });
    }
  }, [activeTab]);

  const handleProviderChange = async (provider) => {
    setSelectedProvider(provider);
    setCostLoading(true);
    try {
      const c = await getCostBreakdown(bucket, provider);
      setCostData(c);
    } catch (e) {
      console.warn("Cost breakdown failed:", e.message);
    }
    setCostLoading(false);
  };

  const costByPrefix = {};
  if (costData) {
    for (const c of costData.children) {
      costByPrefix[c.prefix] = c;
    }
  }

  const loadFolderHistory = async (prefix) => {
    if (expandedFolder === prefix) { setExpandedFolder(null); return; }
    setExpandedFolder(prefix);
    if (folderHistory[prefix]) return;
    try {
      const h = await getStorageHistory(bucket, prefix, 90);
      setFolderHistory(prev => ({ ...prev, [prefix]: h.history || [] }));
    } catch {
      setFolderHistory(prev => ({ ...prev, [prefix]: [] }));
    }
  };

  const totalSize = data ? data.children.reduce((s, c) => s + c.total_size, 0) : 0;
  const totalObjects = data ? data.children.reduce((s, c) => s + c.object_count, 0) : 0;
  const maxSize = data ? Math.max(...data.children.map(c => c.total_size), 1) : 1;
  const sorted = data ? [...data.children].sort((a, b) => b.total_size - a.total_size) : [];
  const barColors = ["#3b82f6", "#22c55e", "#f97316", "#a855f7", "#ef4444", "#06b6d4", "#8b5cf6", "#64748b"];

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal dashboard-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        {/* Header with tabs */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Insights &mdash; {bucket}</h2>
          <div style={{ display: "flex", gap: 4, padding: 3, background: "rgba(255,255,255,0.04)", borderRadius: 8 }}>
            <button style={TAB_STYLE(activeTab === "storage")} onClick={() => setActiveTab("storage")}>Storage</button>
            <button style={TAB_STYLE(activeTab === "optimize")} onClick={() => setActiveTab("optimize")}>Optimize</button>
          </div>
        </div>

        {loading ? (
          <div className="empty"><div className="spinner" /> Analyzing storage...</div>
        ) : error ? (
          <div className="empty" style={{ color: "var(--danger)" }}>Error: {error}</div>
        ) : (
          <>
            {/* STORAGE TAB */}
            {activeTab === "storage" && (
              <div className="dashboard-content">
                <div className="dashboard-summary">
                  <div className="dashboard-card">
                    <div className="dashboard-card-value">{totalObjects.toLocaleString()}</div>
                    <div className="dashboard-card-label">Total Objects</div>
                  </div>
                  <div className="dashboard-card">
                    <div className="dashboard-card-value">{formatSize(totalSize)}</div>
                    <div className="dashboard-card-label">Total Size</div>
                  </div>
                  {costData && (
                    <div className="dashboard-card">
                      <div className="dashboard-card-value">{formatCost(costData.monthly_cost)}<span style={{ fontSize: 13, fontWeight: 400, opacity: 0.6 }}>/mo</span></div>
                      <div className="dashboard-card-label">Est. Monthly Cost</div>
                    </div>
                  )}
                  {costData && (
                    <div className="dashboard-card">
                      <div className="dashboard-card-value">{formatCost(costData.annual_cost)}<span style={{ fontSize: 13, fontWeight: 400, opacity: 0.6 }}>/yr</span></div>
                      <div className="dashboard-card-label">Est. Annual Cost</div>
                    </div>
                  )}
                </div>

                {costData && (
                  <div style={{
                    display: "flex", alignItems: "center", gap: 12, margin: "8px 0 4px", flexWrap: "wrap",
                    padding: "8px 12px", borderRadius: 8, background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)"
                  }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 500 }}>Provider</label>
                    <select
                      value={selectedProvider}
                      onChange={(e) => handleProviderChange(e.target.value)}
                      style={{
                        fontSize: 12, padding: "5px 8px", borderRadius: 6,
                        background: "rgba(255,255,255,0.06)", color: "var(--text, #e4e4e7)",
                        border: "1px solid rgba(255,255,255,0.1)", outline: "none",
                      }}
                      disabled={costLoading}
                    >
                      {Object.entries(PROVIDER_LABELS).filter(([k]) => k !== "unknown").map(([k, v]) => (
                        <option key={k} value={k}>{v}</option>
                      ))}
                    </select>
                    <span style={{ fontSize: 12, color: "var(--text-muted)", opacity: 0.7 }}>
                      ${costData.price_per_gb_month}/GB/mo
                      {costData.provider === "aws" ? " (live)" : ""}
                      {" "}&middot; {costData.pricing_source}
                    </span>
                    {costLoading && <span className="spinner" style={{ width: 14, height: 14 }} />}
                  </div>
                )}

                <div className="dashboard-trend-section">
                  <div className="dashboard-chart-title">Storage Growth Trend</div>
                  <TrendChart history={history} label="Total Storage" />
                </div>

                <div className="dashboard-chart">
                  <div className="dashboard-chart-title">Storage by Folder</div>
                  {sorted.map((child, i) => (
                    <div key={child.prefix}>
                      <div className="dashboard-bar-row"
                        title={`${child.prefix} -- ${formatSize(child.total_size)} (${child.object_count.toLocaleString()} objects)`}>
                        <span className="dashboard-bar-label"
                          onClick={() => child.name !== "(files)" && onNavigate(child.prefix)}
                          style={{ cursor: child.name === "(files)" ? "default" : "pointer" }}>
                          {child.name === "(files)" ? "(root files)" : (child.prefix.replace(/\/$/, "") || "(root files)")}
                        </span>
                        <div className="dashboard-bar-track">
                          <div className="dashboard-bar-fill" style={{ width: `${(child.total_size / maxSize) * 100}%`, background: barColors[i % barColors.length] }} />
                        </div>
                        <span className="dashboard-bar-value">
                          {formatSize(child.total_size)}
                          {costByPrefix[child.prefix] && <span style={{ color: "var(--success, #22c55e)", marginLeft: 6, fontSize: 11 }}>{formatCost(costByPrefix[child.prefix].monthly_cost)}/mo</span>}
                        </span>
                        <button className="btn-small trend-toggle-btn" onClick={() => loadFolderHistory(child.prefix)} title="Show growth trend">
                          {expandedFolder === child.prefix ? "\u25BE" : "\u25B8"}
                        </button>
                      </div>
                      {expandedFolder === child.prefix && (
                        <div className="folder-trend-panel">
                          <TrendChart history={folderHistory[child.prefix]} label={child.prefix.replace(/\/$/, "")} />
                        </div>
                      )}
                    </div>
                  ))}
                </div>

                <div style={{ maxHeight: 300, overflowY: "auto" }}>
                  <table className="dashboard-table">
                    <thead>
                      <tr>
                        <th>Folder</th>
                        <th style={{ textAlign: "right" }}>Objects</th>
                        <th style={{ textAlign: "right" }}>Size</th>
                        {costData && <th style={{ textAlign: "right" }}>Monthly</th>}
                        {costData && <th style={{ textAlign: "right" }}>Annual</th>}
                        <th style={{ textAlign: "right" }}>% of Total</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sorted.map(child => {
                        const pct = totalSize > 0 ? (child.total_size / totalSize * 100) : 0;
                        const cost = costByPrefix[child.prefix];
                        return (
                          <tr key={child.prefix} style={{ cursor: child.name === "(files)" ? "default" : "pointer" }} onClick={() => child.name !== "(files)" && onNavigate(child.prefix)}>
                            <td style={{ fontWeight: 500 }}>{child.name === "(files)" ? "(root files)" : (child.prefix.replace(/\/$/, "") || "(root)")}</td>
                            <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{child.object_count.toLocaleString()}</td>
                            <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{formatSize(child.total_size)}</td>
                            {costData && <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", color: "var(--success, #22c55e)" }}>{cost ? formatCost(cost.monthly_cost) : "--"}</td>}
                            {costData && <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", color: "var(--success, #22c55e)" }}>{cost ? formatCost(cost.annual_cost) : "--"}</td>}
                            <td style={{ textAlign: "right" }}>
                              <span className="dashboard-pct"><span className="dashboard-pct-fill" style={{ width: `${pct}%` }} /></span>
                              {pct.toFixed(1)}%
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                {costData && costData.class_comparison && Object.keys(costData.class_comparison).length > 1 && (
                  <div style={{ marginTop: 16 }}>
                    <div className="dashboard-chart-title">Storage Class Comparison &mdash; {PROVIDER_LABELS[costData.provider] || costData.provider}</div>
                    <table className="dashboard-table">
                      <thead>
                        <tr>
                          <th>Storage Class</th>
                          <th style={{ textAlign: "right" }}>$/GB/mo</th>
                          <th style={{ textAlign: "right" }}>Monthly ({formatSize(costData.total_size)})</th>
                          <th style={{ textAlign: "right" }}>Annual</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(costData.class_comparison)
                          .sort(([,a], [,b]) => b.monthly_cost - a.monthly_cost)
                          .map(([cls, info]) => (
                          <tr key={cls}>
                            <td style={{ fontWeight: 500 }}>{cls.replace(/_/g, " ")}</td>
                            <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>${info.price_per_gb_month}</td>
                            <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{formatCost(info.monthly_cost)}</td>
                            <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{formatCost(info.annual_cost)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

            {/* OPTIMIZE TAB */}
            {activeTab === "optimize" && (
              <div className="dashboard-content">
                {optLoading && <div className="empty"><div className="spinner" /> Analyzing optimization opportunities...</div>}

                {optError && (
                  <p className="muted" style={{ color: "var(--danger, #ef4444)" }}>Failed to load: {optError}</p>
                )}

                {optData && optData.total_objects === 0 && (
                  <p className="muted">This bucket is empty. No optimization recommendations available.</p>
                )}

                {optData && optData.total_objects > 0 && (
                  <>
                    {/* Recommendations */}
                    {optData.lifecycle.recommendations.length > 0 ? (
                      <div style={{ marginBottom: 20 }}>
                        <div className="dashboard-chart-title">Recommendations</div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                          {optData.lifecycle.recommendations.map((rec, i) => {
                            const s = SEVERITY_STYLES[rec.severity] || SEVERITY_STYLES.low;
                            return (
                              <div key={i} style={{ padding: "10px 14px", borderRadius: 8, background: s.bg, border: `1px solid ${s.border}` }}>
                                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
                                  <span style={{ fontSize: 11, fontWeight: 600, color: s.color, padding: "1px 6px", borderRadius: 4, background: s.bg, border: `1px solid ${s.border}` }}>{s.label}</span>
                                  <span style={{ fontSize: 13, fontWeight: 500 }}>{rec.message}</span>
                                </div>
                                <div style={{ fontSize: 12, color: "var(--text-muted)", paddingLeft: 2 }}>{rec.suggestion}</div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    ) : (
                      <div style={{ padding: "12px 14px", borderRadius: 8, marginBottom: 20, background: "rgba(34, 197, 94, 0.06)", border: "1px solid rgba(34, 197, 94, 0.2)" }}>
                        <span style={{ fontSize: 13, color: "var(--success, #22c55e)", fontWeight: 500 }}>All clear &mdash; no optimization issues found for this bucket.</span>
                      </div>
                    )}

                    {/* Tiering Savings */}
                    {optData.tiering && optData.tiering.recommended_class && (
                      <div style={{ padding: "14px 16px", borderRadius: 8, marginBottom: 20, background: "rgba(34, 197, 94, 0.04)", border: "1px solid rgba(34, 197, 94, 0.15)" }}>
                        <div className="dashboard-chart-title" style={{ marginBottom: 8 }}>Tiering Opportunity</div>
                        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, textAlign: "center" }}>
                          <div>
                            <div style={{ fontSize: 22, fontWeight: 600 }}>{formatCost(optData.tiering.monthly_savings)}</div>
                            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Monthly Savings</div>
                          </div>
                          <div>
                            <div style={{ fontSize: 22, fontWeight: 600 }}>{formatCost(optData.tiering.annual_savings)}</div>
                            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Annual Savings</div>
                          </div>
                          <div>
                            <div style={{ fontSize: 22, fontWeight: 600 }}>{optData.tiering.savings_pct}%</div>
                            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Cost Reduction</div>
                          </div>
                        </div>
                        <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 10, textAlign: "center" }}>
                          Move {formatSize(optData.tiering.cold_data_size)} ({optData.tiering.cold_data_pct}% of bucket) to <strong>{optData.tiering.recommended_class.replace(/_/g, " ")}</strong>
                        </div>
                      </div>
                    )}

                    {/* Lifecycle Status */}
                    <div style={{ marginBottom: 20 }}>
                      <div className="dashboard-chart-title">Lifecycle Status</div>
                      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                        {[
                          { label: "Rules", value: optData.lifecycle.rule_count, ok: optData.lifecycle.rule_count > 0 },
                          { label: "Expiration", value: optData.lifecycle.has_expiration ? "Yes" : "No", ok: optData.lifecycle.has_expiration },
                          { label: "Version Cleanup", value: optData.lifecycle.has_noncurrent ? "Yes" : "No", ok: optData.lifecycle.has_noncurrent },
                          { label: "Abort Uploads", value: optData.lifecycle.has_abort ? "Yes" : "No", ok: optData.lifecycle.has_abort },
                          { label: "Tiering", value: optData.lifecycle.has_transition ? "Yes" : "No", ok: optData.lifecycle.has_transition },
                        ].map((item, i) => (
                          <div key={i} style={{
                            padding: "8px 14px", borderRadius: 8, textAlign: "center", minWidth: 90,
                            background: item.ok ? "rgba(34, 197, 94, 0.06)" : "rgba(255,255,255,0.03)",
                            border: item.ok ? "1px solid rgba(34, 197, 94, 0.2)" : "1px solid rgba(255,255,255,0.06)",
                          }}>
                            <div style={{ fontSize: 16, fontWeight: 600, color: item.ok ? "var(--success, #22c55e)" : "var(--text-muted)" }}>{item.value}</div>
                            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{item.label}</div>
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Age Distribution */}
                    <div style={{ marginBottom: 20 }}>
                      <div className="dashboard-chart-title">Data Age Distribution</div>
                      <div style={{ display: "flex", flexDirection: "column", gap: 4, padding: "12px 14px", borderRadius: 8, background: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.06)" }}>
                        {optData.age_distribution.map((a, i) => (
                          <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13 }}>
                            <span style={{ width: 50, textAlign: "right", color: "var(--text-muted)", flexShrink: 0 }}>&gt;{a.older_than_days}d</span>
                            <div style={{ flex: 1, height: 20, background: "rgba(255,255,255,0.04)", borderRadius: 4, overflow: "hidden" }}>
                              <div style={{
                                width: `${Math.max(a.pct_size, 0.5)}%`, height: "100%", borderRadius: 4,
                                background: a.pct_size > 50 ? "rgba(239, 68, 68, 0.5)" : a.pct_size > 20 ? "rgba(245, 158, 11, 0.4)" : "rgba(59, 130, 246, 0.3)",
                              }} />
                            </div>
                            <span style={{ width: 80, textAlign: "right", fontVariantNumeric: "tabular-nums", flexShrink: 0 }}>{formatSize(a.total_size)}</span>
                            <span style={{ width: 50, textAlign: "right", color: "var(--text-muted)", fontVariantNumeric: "tabular-nums", flexShrink: 0 }}>{a.pct_size}%</span>
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Cold Data by Folder */}
                    {optData.cold_data.folders.length > 0 && (
                      <div style={{ marginBottom: 20 }}>
                        <div className="dashboard-chart-title">
                          Cold Data by Folder <span style={{ fontWeight: 400, color: "var(--text-muted)", fontSize: 12 }}>(&gt;{optData.cold_data.threshold_days} days old)</span>
                        </div>
                        <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>
                          {formatSize(optData.cold_data.total_cold_size)} total cold data ({optData.cold_data.cold_pct}% of bucket)
                        </p>
                        <table className="dashboard-table" style={{ fontSize: 13 }}>
                          <thead><tr><th>Folder</th><th style={{ textAlign: "right" }}>Cold</th><th style={{ textAlign: "right" }}>Total</th><th style={{ textAlign: "right" }}>Cold %</th></tr></thead>
                          <tbody>
                            {optData.cold_data.folders.slice(0, 15).map((f, i) => (
                              <tr key={i}>
                                <td style={{ fontWeight: 500 }}>{f.folder}</td>
                                <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{formatSize(f.cold_size)}</td>
                                <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", color: "var(--text-muted)" }}>{formatSize(f.total_size)}</td>
                                <td style={{ textAlign: "right" }}>
                                  <span style={{ color: f.cold_pct > 80 ? "#ef4444" : f.cold_pct > 40 ? "#f59e0b" : "var(--text-muted)", fontWeight: f.cold_pct > 50 ? 600 : 400 }}>{f.cold_pct}%</span>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}

                    {/* Duplicates */}
                    <div style={{ marginBottom: 20 }}>
                      <div className="dashboard-chart-title">Duplicate Files</div>
                      {optData.duplicates.groups.length > 0 ? (
                        <>
                          <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>
                            {optData.duplicates.groups.length} duplicate group{optData.duplicates.groups.length !== 1 ? "s" : ""} wasting {formatSize(optData.duplicates.total_wasted)}
                          </p>
                          <table className="dashboard-table" style={{ fontSize: 13 }}>
                            <thead><tr><th>Filename</th><th style={{ textAlign: "right" }}>Size</th><th style={{ textAlign: "right" }}>Copies</th><th style={{ textAlign: "right" }}>Wasted</th></tr></thead>
                            <tbody>
                              {optData.duplicates.groups.slice(0, 10).map((d, i) => (
                                <tr key={i}>
                                  <td className="mono" style={{ wordBreak: "break-all" }}>{d.filename.length > 60 ? "..." + d.filename.slice(-57) : d.filename}</td>
                                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{formatSize(d.size)}</td>
                                  <td style={{ textAlign: "right" }}>{d.copies}</td>
                                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", color: "#ef4444" }}>{formatSize(d.wasted_bytes)}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </>
                      ) : (
                        <p className="muted" style={{ fontSize: 12 }}>{optData.duplicates.skipped ? "Duplicate scan skipped for buckets with over 1M objects." : "No duplicate files detected."}</p>
                      )}
                    </div>

                    {/* Disclaimer */}
                    <div style={{ padding: "8px 12px", borderRadius: 6, background: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.05)", fontSize: 11, color: "var(--text-muted)", lineHeight: 1.5 }}>
                      <strong>Note:</strong> "Cold" data is based on last-modified time, not access patterns &mdash; S3 does not expose read timestamps.
                      Duplicate detection uses filename + size matching, not checksums.
                      Cost estimates use published provider pricing and may not reflect negotiated rates or free-tier credits.
                      Always verify recommendations against your retention and compliance requirements before applying lifecycle rules.
                    </div>
                  </>
                )}
              </div>
            )}
          </>
        )}

        <div className="modal-actions">
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
