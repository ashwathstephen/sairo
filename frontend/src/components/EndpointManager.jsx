import React, { useState, useEffect } from "react";
import { listEndpoints, createEndpoint, updateEndpoint, deleteEndpoint, testEndpoint } from "../api";

export default function EndpointManager({ onClose }) {
  const [endpoints, setEndpoints] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(null); // null = list, "new" = add form, {id} = edit form
  const [form, setForm] = useState({ id: "", name: "", endpoint_url: "", access_key: "", secret_key: "", region: "", path_style: false });
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(null); // endpoint id being tested
  const [testResult, setTestResult] = useState(null);

  const fetchEndpoints = async () => {
    setLoading(true);
    try {
      const data = await listEndpoints();
      setEndpoints(data.endpoints || []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchEndpoints(); }, []);

  const resetForm = () => {
    setForm({ id: "", name: "", endpoint_url: "", access_key: "", secret_key: "", region: "", path_style: false });
    setEditing(null);
    setError(null);
    setTestResult(null);
  };

  const startEdit = (ep) => {
    setForm({
      id: ep.id,
      name: ep.name,
      endpoint_url: ep.endpoint_url,
      access_key: "", // don't prefill secrets
      secret_key: "",
      region: ep.region || "",
      path_style: !!ep.path_style,
    });
    setEditing(ep.id);
    setError(null);
    setTestResult(null);
  };

  const handleSave = async () => {
    setError(null);
    setSaving(true);
    try {
      if (editing === "new") {
        if (!form.id.trim() || !form.name.trim() || !form.endpoint_url.trim() || !form.access_key.trim() || !form.secret_key.trim()) {
          throw new Error("ID, Name, URL, Access Key, and Secret Key are required");
        }
        await createEndpoint({
          id: form.id.trim(),
          name: form.name.trim(),
          endpoint_url: form.endpoint_url.trim(),
          access_key: form.access_key.trim(),
          secret_key: form.secret_key.trim(),
          region: form.region.trim(),
          path_style: form.path_style,
        });
      } else {
        const payload = {
          name: form.name.trim(),
          endpoint_url: form.endpoint_url.trim(),
          region: form.region.trim(),
          path_style: form.path_style,
        };
        // Only include secrets if user typed new ones
        if (form.access_key.trim()) payload.access_key = form.access_key.trim();
        if (form.secret_key.trim()) payload.secret_key = form.secret_key.trim();
        await updateEndpoint(editing, payload);
      }
      resetForm();
      fetchEndpoints();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id) => {
    if (!confirm(`Delete endpoint "${id}"? This cannot be undone.`)) return;
    try {
      await deleteEndpoint(id);
      fetchEndpoints();
    } catch (e) {
      setError(e.message);
    }
  };

  const handleTest = async (id) => {
    setTesting(id);
    setTestResult(null);
    try {
      const result = await testEndpoint(id);
      setTestResult({ id, success: result.status === "ok", detail: result.detail || `Connected — ${result.buckets} bucket(s)` });
    } catch (e) {
      setTestResult({ id, success: false, detail: e.message });
    } finally {
      setTesting(null);
    }
  };

  const isEditing = editing !== null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()} style={{ width: 600, maxHeight: "80vh", overflow: "auto" }}>
        <h2>S3 Endpoints</h2>

        {error && <div className="ep-error">{error}</div>}

        {!isEditing && (
          <>
            {loading ? (
              <div style={{ padding: 16, textAlign: "center" }}><div className="spinner" /> Loading...</div>
            ) : (
              <div className="ep-list">
                {endpoints.map((ep) => (
                  <div key={ep.id} className="ep-card">
                    <div className="ep-card-header">
                      <span className="ep-card-name">{ep.name}</span>
                      {ep.is_default && <span className="ep-badge-default">default</span>}
                    </div>
                    <div className="ep-card-url">{ep.endpoint_url}</div>
                    <div className="ep-card-meta">
                      {ep.region && <span>Region: {ep.region}</span>}
                      <span>Access Key: {ep.access_key_masked}</span>
                    </div>
                    {testResult && testResult.id === ep.id && (
                      <div className={`ep-test-result ${testResult.success ? "ep-test-pass" : "ep-test-fail"}`}>
                        {testResult.success ? "\u2713" : "\u2717"} {testResult.detail}
                      </div>
                    )}
                    <div className="ep-card-actions">
                      <button
                        className="btn-settings btn-xs"
                        onClick={() => handleTest(ep.id)}
                        disabled={testing === ep.id}
                      >
                        {testing === ep.id ? "Testing..." : "Test Connection"}
                      </button>
                      <button className="btn-settings btn-xs" onClick={() => startEdit(ep)}>Edit</button>
                      {!ep.is_default && (
                        <button className="btn-danger btn-xs" onClick={() => handleDelete(ep.id)}>Delete</button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div className="modal-actions">
              <button onClick={onClose}>Close</button>
              <button className="btn-primary" onClick={() => { setEditing("new"); setError(null); setTestResult(null); }}>
                + Add Endpoint
              </button>
            </div>
          </>
        )}

        {isEditing && (
          <div className="ep-form">
            {editing === "new" && (
              <div className="ep-form-field">
                <label>ID (slug)</label>
                <input
                  type="text"
                  value={form.id}
                  onChange={(e) => setForm({ ...form, id: e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "") })}
                  placeholder="my-wasabi"
                  autoFocus
                />
                <span className="ep-form-hint">Lowercase letters, numbers, hyphens, underscores only</span>
              </div>
            )}
            <div className="ep-form-field">
              <label>Display Name</label>
              <input type="text" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Wasabi US East" />
            </div>
            <div className="ep-form-field">
              <label>Endpoint URL</label>
              <input type="text" value={form.endpoint_url} onChange={(e) => setForm({ ...form, endpoint_url: e.target.value })} placeholder="https://s3.wasabisys.com" />
            </div>
            <div className="ep-form-row">
              <div className="ep-form-field" style={{ flex: 1 }}>
                <label>Access Key</label>
                <input type="text" value={form.access_key} onChange={(e) => setForm({ ...form, access_key: e.target.value })} placeholder={editing !== "new" ? "(unchanged)" : ""} />
              </div>
              <div className="ep-form-field" style={{ flex: 1 }}>
                <label>Secret Key</label>
                <input type="password" value={form.secret_key} onChange={(e) => setForm({ ...form, secret_key: e.target.value })} placeholder={editing !== "new" ? "(unchanged)" : ""} />
              </div>
            </div>
            <div className="ep-form-row">
              <div className="ep-form-field" style={{ flex: 1 }}>
                <label>Region</label>
                <input type="text" value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value })} placeholder="us-east-1" />
              </div>
              <div className="ep-form-field" style={{ flex: 1, display: "flex", alignItems: "center", gap: 8, paddingTop: 20 }}>
                <input type="checkbox" checked={form.path_style} onChange={(e) => setForm({ ...form, path_style: e.target.checked })} id="path-style" />
                <label htmlFor="path-style" style={{ marginBottom: 0 }}>Path-style addressing</label>
              </div>
            </div>
            <div className="modal-actions">
              <button onClick={resetForm}>Cancel</button>
              <button className="btn-primary" onClick={handleSave} disabled={saving}>
                {saving ? "Saving..." : editing === "new" ? "Add Endpoint" : "Save Changes"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
