import React, { useState, useEffect } from "react";
import ConfirmDialog from "./ConfirmDialog";
import { toast } from "./Toast";
import { listAllBuckets, createBucket, deleteBucket, formatSize, formatDate, setCurrentEndpoint } from "../api";

export default function BucketList({ onSelect, role, onDashboard }) {
  const isAdmin = role === "admin";
  const [endpoints, setEndpoints] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState(null);
  const [deletingBucket, setDeletingBucket] = useState(null);
  const [deletingEndpoint, setDeletingEndpoint] = useState(null);
  const [collapsed, setCollapsed] = useState({});
  const [createEndpointId, setCreateEndpointId] = useState("default");

  const multiEndpoint = endpoints.length > 1;
  const totalBuckets = endpoints.reduce((sum, ep) => sum + (ep.buckets?.length || 0), 0);

  const fetchBuckets = async () => {
    setLoading(true);
    try {
      const d = await listAllBuckets();
      setEndpoints(d.endpoints || []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchBuckets();
    const interval = setInterval(fetchBuckets, 30000);
    return () => clearInterval(interval);
  }, []);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      // Temporarily set endpoint for the create call
      setCurrentEndpoint(createEndpointId);
      await createBucket(newName.trim());
      setCurrentEndpoint("default");
      toast(`Bucket "${newName.trim()}" created`, "success");
      setNewName("");
      setShowCreate(false);
      fetchBuckets();
    } catch (e) {
      setCurrentEndpoint("default");
      setError(e.message);
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (name, endpointId) => {
    try {
      setCurrentEndpoint(endpointId);
      await deleteBucket(name);
      setCurrentEndpoint("default");
      setDeletingBucket(null);
      setDeletingEndpoint(null);
      toast(`Bucket "${name}" deleted`, "success");
      fetchBuckets();
    } catch (e) {
      setCurrentEndpoint("default");
      setDeletingBucket(null);
      setDeletingEndpoint(null);
      toast(`Failed to delete bucket: ${e.message}`, "error");
    }
  };

  const handleSelectBucket = (bucketName, permission, endpointId) => {
    setCurrentEndpoint(endpointId);
    onSelect(bucketName, permission, endpointId);
  };

  const toggleCollapse = (epId) => {
    setCollapsed(prev => ({ ...prev, [epId]: !prev[epId] }));
  };

  if (loading) {
    return <div className="empty"><div className="spinner" /> Loading buckets...</div>;
  }

  if (error && endpoints.length === 0) {
    return <div className="empty" style={{ color: "var(--danger)" }}>Error: {error}</div>;
  }

  return (
    <div>
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <span className="count">
          {totalBuckets} bucket{totalBuckets !== 1 ? "s" : ""}
          {multiEndpoint && ` across ${endpoints.length} endpoints`}
        </span>
        {isAdmin && <button onClick={() => setShowCreate(!showCreate)} className="btn-primary">+ Create Bucket</button>}
      </div>

      {showCreate && (
        <div className="create-bucket-bar">
          {multiEndpoint && (
            <select
              value={createEndpointId}
              onChange={(e) => setCreateEndpointId(e.target.value)}
              style={{ padding: "6px 10px", border: "1px solid var(--border-input)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text)" }}
            >
              {endpoints.map(ep => <option key={ep.endpoint_id} value={ep.endpoint_id}>{ep.endpoint_name}</option>)}
            </select>
          )}
          <input
            type="text"
            placeholder="new-bucket-name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            className="filter-input"
            onKeyDown={(e) => e.key === "Enter" && handleCreate()}
            autoFocus
          />
          <button onClick={handleCreate} disabled={creating} className="btn-primary">
            {creating ? "Creating..." : "Create"}
          </button>
          <button onClick={() => setShowCreate(false)}>Cancel</button>
        </div>
      )}

      {totalBuckets === 0 && !showCreate && (
        <div className="empty-state">
          <div className="empty-state-icon">&#128230;</div>
          <h3 className="empty-state-title">No buckets yet</h3>
          <p className="empty-state-text">{isAdmin ? "Create your first bucket to get started" : "No buckets are available"}</p>
          {isAdmin && (
            <div className="empty-state-actions">
              <button onClick={() => setShowCreate(true)} className="btn-primary">+ Create Bucket</button>
            </div>
          )}
        </div>
      )}

      {endpoints.map((ep) => (
        <div key={ep.endpoint_id} className="ep-section">
          {multiEndpoint && (
            <div className="ep-section-header" onClick={() => toggleCollapse(ep.endpoint_id)}>
              <span className="ep-section-toggle">{collapsed[ep.endpoint_id] ? "\u25b6" : "\u25bc"}</span>
              <span className="ep-section-name">{ep.endpoint_name}</span>
              <span className="ep-section-count">{ep.buckets.length} bucket{ep.buckets.length !== 1 ? "s" : ""}</span>
              {ep.endpoint_id !== "default" && <span className="ep-section-id">{ep.endpoint_id}</span>}
            </div>
          )}
          {!collapsed[ep.endpoint_id] && (
            <div className="bucket-grid">
              {ep.buckets.map((b) => (
                <div key={`${ep.endpoint_id}:${b.name}`} className="bucket-card" onClick={() => handleSelectBucket(b.name, b.permission, ep.endpoint_id)}>
                  <div className="bucket-card-header">
                    <span className="bucket-card-icon">
                      <svg viewBox="0 0 40 40" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" width="18" height="18">
                        <path d="M28 10c0 3-4 5.5-8 5.5S12 13 12 10s4-5.5 8-5.5 8 2.5 8 5.5z"/><path d="M28 20c0 3-4 5.5-8 5.5S12 23 12 20"/><path d="M28 30c0 3-4 5.5-8 5.5S12 33 12 30"/><line x1="12" y1="10" x2="12" y2="30"/><line x1="28" y1="10" x2="28" y2="30"/>
                      </svg>
                    </span>
                    <span className="bucket-card-name">{b.name}</span>
                    {multiEndpoint && <span className="ep-inline-badge">{ep.endpoint_name}</span>}
                  </div>
                  <div className="bucket-card-meta">
                    <span>Created: {formatDate(b.created)}</span>
                    {(b.index_status === "complete" || b.indexed) && (
                      <>
                        <span>{(b.object_count || 0).toLocaleString()} objects</span>
                        <span>{formatSize(b.total_size || 0)}</span>
                      </>
                    )}
                    {b.index_status === "crawling" && (
                      <span className="crawl-active" style={{ fontSize: 11, padding: "1px 6px" }}>
                        {b.indexed ? "Refreshing index…" : `Indexing... ${(b.object_count || 0).toLocaleString()}`}
                      </span>
                    )}
                    {!b.index_status && (
                      <span style={{ color: "var(--text-light)", fontSize: 12 }}>Not indexed</span>
                    )}
                  </div>
                  <div className="bucket-card-actions">
                    {onDashboard && b.index_status === "complete" && <button
                      className="btn-settings btn-xs"
                      onClick={(e) => { e.stopPropagation(); setCurrentEndpoint(ep.endpoint_id); onDashboard(b.name); }}
                      title="Storage Dashboard"
                    >
                      &#128202;
                    </button>}
                    {isAdmin && <button
                      className="btn-danger btn-xs bucket-delete-btn"
                      onClick={(e) => { e.stopPropagation(); setDeletingBucket(b.name); setDeletingEndpoint(ep.endpoint_id); }}
                      title="Delete bucket"
                    >
                      Delete
                    </button>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
      {deletingBucket && (
        <ConfirmDialog
          title="Delete Bucket"
          message={<p>Delete bucket <strong>"{deletingBucket}"</strong>? It must be empty.</p>}
          confirmLabel="Delete"
          variant="danger"
          onConfirm={() => handleDelete(deletingBucket, deletingEndpoint || "default")}
          onCancel={() => { setDeletingBucket(null); setDeletingEndpoint(null); }}
        />
      )}
    </div>
  );
}
