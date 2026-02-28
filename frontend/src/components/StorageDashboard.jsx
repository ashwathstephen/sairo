import React, { useState, useEffect } from "react";
import { getStorageBreakdown, getStorageHistory, formatSize } from "../api";

function formatPrecise(bytes, refMin, refMax) {
  // Format with enough precision so Y-axis ticks never show duplicate labels.
  // Picks a unit based on refMax, then uses enough decimals to distinguish refMin from refMax.
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.floor(Math.log(Math.abs(refMax || bytes)) / Math.log(1024));
  const val = bytes / Math.pow(1024, i);
  const rangeInUnit = (refMax - refMin) / Math.pow(1024, i);
  // Use enough decimals so the range spans at least 4 distinct labels
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
  // When flat, create a ±2% range around the value so the chart isn't a single line with repeated labels
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

  // Y-axis labels (5 ticks) — use precise formatter to avoid duplicates
  const yTicks = Array.from({ length: 5 }, (_, i) => {
    const val = min + (range * i) / 4;
    const y = PAD_Y + chartH - (i / 4) * chartH;
    return { val, y, label: formatPrecise(val, min, max) };
  });

  // X-axis labels — show time if all data is from the same day, otherwise show date
  const firstDate = new Date(history[0].timestamp).toDateString();
  const lastDate = new Date(history[history.length - 1].timestamp).toDateString();
  const sameDay = firstDate === lastDate;

  // Pick ~5 evenly spaced labels (avoiding overlap)
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
        {/* Grid lines */}
        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={PAD_X} y1={t.y} x2={W - 10} y2={t.y} stroke="var(--border)" strokeWidth="0.5" strokeDasharray="4,4" />
            <text x={PAD_X - 6} y={t.y + 4} textAnchor="end" fill="var(--text-muted)" fontSize="9">{t.label}</text>
          </g>
        ))}
        {/* X-axis labels */}
        {xLabels.map((l, i) => (
          <text key={i} x={l.x} y={H - 5} textAnchor="middle" fill="var(--text-muted)" fontSize="9">{l.label}</text>
        ))}
        {/* Area fill */}
        <path d={areaPath} fill="rgba(59,130,246,0.1)" />
        {/* Line */}
        <path d={linePath} fill="none" stroke="#3b82f6" strokeWidth="2" strokeLinejoin="round" />
        {/* Hover crosshair */}
        {hovered !== null && (
          <line x1={points[hovered].x} y1={PAD_Y} x2={points[hovered].x} y2={PAD_Y + chartH}
            stroke="var(--text-muted)" strokeWidth="0.5" strokeDasharray="3,3" />
        )}
        {/* Data points */}
        {points.map((p, i) => (
          <circle key={i} cx={p.x} cy={p.y} r={hovered === i ? 5 : 3}
            fill="#3b82f6" stroke="var(--bg-card, #1e1e2e)" strokeWidth="1.5"
            onMouseEnter={() => setHovered(i)} style={{ cursor: "pointer" }} />
        ))}
        {/* Hover tooltip */}
        {hovered !== null && (() => {
          const p = points[hovered];
          const ts = new Date(p.ts);
          const timeStr = sameDay
            ? ts.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })
            : ts.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
              ts.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
          const text = `${timeStr}: ${formatPrecise(p.val, rawMin, rawMax)} (${p.count.toLocaleString()} objects)`;
          // Position tooltip to avoid edge overflow
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

export default function StorageDashboard({ bucket, onClose, onNavigate }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [history, setHistory] = useState(null);
  const [folderHistory, setFolderHistory] = useState({});
  const [expandedFolder, setExpandedFolder] = useState(null);

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
      })
      .catch(e => { setError(e.message); setLoading(false); });
  }, [bucket]);

  const loadFolderHistory = async (prefix) => {
    if (expandedFolder === prefix) {
      setExpandedFolder(null);
      return;
    }
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

  const formatCount = (n) => n.toLocaleString();

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal dashboard-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>Storage Dashboard — {bucket}</h2>

        {loading ? (
          <div className="empty"><div className="spinner" /> Analyzing storage...</div>
        ) : error ? (
          <div className="empty" style={{ color: "var(--danger)" }}>Error: {error}</div>
        ) : (
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
              <div className="dashboard-card">
                <div className="dashboard-card-value">{sorted.length}</div>
                <div className="dashboard-card-label">Top-Level Folders</div>
              </div>
              {sorted.length > 0 && (
                <div className="dashboard-card">
                  <div className="dashboard-card-value">{formatSize(sorted[0].total_size)}</div>
                  <div className="dashboard-card-label">Largest: {sorted[0].prefix.replace(/\/$/, "")}</div>
                </div>
              )}
            </div>

            {/* Bucket-level growth trend */}
            <div className="dashboard-trend-section">
              <div className="dashboard-chart-title">Storage Growth Trend</div>
              <TrendChart history={history} label="Total Storage" />
            </div>

            <div className="dashboard-chart">
              <div className="dashboard-chart-title">Storage by Folder</div>
              {sorted.map((child, i) => (
                <div key={child.prefix}>
                  <div
                    className="dashboard-bar-row"
                    title={`${child.prefix} — ${formatSize(child.total_size)} (${child.object_count.toLocaleString()} objects)`}
                  >
                    <span
                      className="dashboard-bar-label"
                      onClick={() => child.name !== "(files)" && onNavigate(child.prefix)}
                      style={{ cursor: child.name === "(files)" ? "default" : "pointer" }}
                    >
                      {child.name === "(files)" ? "(root files)" : (child.prefix.replace(/\/$/, "") || "(root files)")}
                    </span>
                    <div className="dashboard-bar-track">
                      <div
                        className="dashboard-bar-fill"
                        style={{
                          width: `${(child.total_size / maxSize) * 100}%`,
                          background: barColors[i % barColors.length],
                        }}
                      />
                    </div>
                    <span className="dashboard-bar-value">{formatSize(child.total_size)}</span>
                    <button
                      className="btn-small trend-toggle-btn"
                      onClick={() => loadFolderHistory(child.prefix)}
                      title="Show growth trend"
                      aria-label={`Show trend for ${child.prefix}`}
                    >
                      {expandedFolder === child.prefix ? "▾" : "▸"}
                    </button>
                  </div>
                  {expandedFolder === child.prefix && (
                    <div className="folder-trend-panel">
                      <TrendChart
                        history={folderHistory[child.prefix]}
                        label={child.prefix.replace(/\/$/, "")}
                      />
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
                    <th style={{ textAlign: "right" }}>% of Total</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map(child => {
                    const pct = totalSize > 0 ? (child.total_size / totalSize * 100) : 0;
                    return (
                      <tr key={child.prefix} style={{ cursor: child.name === "(files)" ? "default" : "pointer" }} onClick={() => child.name !== "(files)" && onNavigate(child.prefix)}>
                        <td style={{ fontWeight: 500 }}>{child.name === "(files)" ? "(root files)" : (child.prefix.replace(/\/$/, "") || "(root)")}</td>
                        <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{child.object_count.toLocaleString()}</td>
                        <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{formatSize(child.total_size)}</td>
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
          </div>
        )}

        <div className="modal-actions">
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
