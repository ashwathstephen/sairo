import React, { useState, useEffect } from "react";
import { listAllBuckets, streamList, setCurrentEndpoint, getCurrentEndpoint } from "../api";

export default function FolderPicker({ currentBucket, currentPrefix, action, onSelect, onClose }) {
  const [endpointId, setEndpointId] = useState(getCurrentEndpoint() || "default");
  const [bucket, setBucket] = useState(currentBucket);
  const [prefix, setPrefix] = useState("");
  const [typedPath, setTypedPath] = useState("");
  const [folders, setFolders] = useState([]);
  const [endpoints, setEndpoints] = useState([]);
  const [buckets, setBuckets] = useState([]);
  const [loading, setLoading] = useState(false);

  const multiEndpoint = endpoints.length > 1;

  useEffect(() => {
    listAllBuckets().then(d => {
      const eps = d.endpoints || [];
      setEndpoints(eps);
      // Build bucket list for current endpoint
      const ep = eps.find(e => e.endpoint_id === endpointId);
      setBuckets(ep ? ep.buckets.map(b => b.name) : []);
    }).catch(() => {});
  }, []);

  // Update bucket list when endpoint changes
  useEffect(() => {
    const ep = endpoints.find(e => e.endpoint_id === endpointId);
    const names = ep ? ep.buckets.map(b => b.name) : [];
    setBuckets(names);
    if (names.length > 0 && !names.includes(bucket)) {
      setBucket(names[0]);
      setPrefix("");
      setTypedPath("");
    }
  }, [endpointId, endpoints]);

  useEffect(() => {
    if (!bucket) return;
    setLoading(true);
    setFolders([]);
    // Temporarily set endpoint for this listing
    const prevEndpoint = getCurrentEndpoint();
    setCurrentEndpoint(endpointId);
    const controller = streamList(bucket, prefix, (page) => {
      if (page.folders.length > 0) setFolders(prev => [...prev, ...page.folders]);
      if (page.done) setLoading(false);
    });
    return () => {
      controller?.abort();
      setCurrentEndpoint(prevEndpoint);
    };
  }, [bucket, prefix, endpointId]);

  const navigate = (folder) => {
    setPrefix(folder);
    setTypedPath(folder);
  };

  const goUp = () => {
    if (!prefix) return;
    const parts = prefix.split("/").filter(Boolean);
    parts.pop();
    const newPrefix = parts.length > 0 ? parts.join("/") + "/" : "";
    setPrefix(newPrefix);
    setTypedPath(newPrefix);
  };

  const handleSelect = () => {
    // Set the endpoint context before calling onSelect
    setCurrentEndpoint(endpointId);
    onSelect(bucket, prefix || "", endpointId);
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()} style={{ width: 520 }}>
        <h2>{action === "copy" ? "Copy to..." : "Move to..."}</h2>

        {multiEndpoint && (
          <div style={{ marginBottom: 8 }}>
            <label style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4, display: "block" }}>Endpoint</label>
            <select
              value={endpointId}
              onChange={(e) => { setEndpointId(e.target.value); setPrefix(""); setTypedPath(""); }}
              style={{ width: "100%", padding: "6px 10px", border: "1px solid var(--border-input)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text)" }}
            >
              {endpoints.map(ep => <option key={ep.endpoint_id} value={ep.endpoint_id}>{ep.endpoint_name}</option>)}
            </select>
          </div>
        )}

        <div style={{ marginBottom: 8 }}>
          <label style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4, display: "block" }}>Bucket</label>
          <select
            value={bucket}
            onChange={(e) => { setBucket(e.target.value); setPrefix(""); setTypedPath(""); }}
            style={{ width: "100%", padding: "6px 10px", border: "1px solid var(--border-input)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text)" }}
          >
            {buckets.map(b => <option key={b} value={b}>{b}</option>)}
          </select>
        </div>

        <div className="folder-picker-path">
          <input
            type="text"
            value={typedPath}
            onChange={(e) => setTypedPath(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") setPrefix(typedPath); }}
            placeholder="Type a path or browse below..."
          />
          <button onClick={() => setPrefix(typedPath)} style={{ padding: "6px 10px" }}>Go</button>
        </div>

        {prefix && (
          <div className="folder-picker-item" onClick={goUp} style={{ fontWeight: 500 }}>
            <span>&#8593;</span> <span>.. (up one level)</span>
          </div>
        )}

        <div className="folder-picker-list">
          {loading ? (
            <div style={{ padding: 16, textAlign: "center", color: "var(--text-muted)" }}><div className="spinner" /> Loading...</div>
          ) : folders.length === 0 ? (
            <div style={{ padding: 16, textAlign: "center", color: "var(--text-light)", fontSize: 13 }}>No subfolders</div>
          ) : (
            folders.map(f => (
              <div key={f.prefix} className="folder-picker-item" onClick={() => navigate(f.prefix)}>
                <span>&#128193;</span>
                <span>{f.name}/</span>
              </div>
            ))
          )}
        </div>

        <div style={{ marginTop: 8, fontSize: 12, color: "var(--text-muted)" }}>
          Destination: {multiEndpoint ? `[${endpointId}] ` : ""}{bucket}/{prefix || "(root)"}
        </div>

        <div className="modal-actions">
          <button onClick={onClose}>Cancel</button>
          <button className="btn-primary" onClick={handleSelect}>
            {action === "copy" ? "Copy here" : "Move here"}
          </button>
        </div>
      </div>
    </div>
  );
}
