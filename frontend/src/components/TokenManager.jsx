import React, { useState, useEffect } from "react";
import { listTokens, createToken, deleteToken, formatDate } from "../api";
import ConfirmDialog from "./ConfirmDialog";

export default function TokenManager({ onClose }) {
  const [tokens, setTokens] = useState([]);
  const [loading, setLoading] = useState(true);
  const [newName, setNewName] = useState("");
  const [newRole, setNewRole] = useState("viewer");
  const [newExpiry, setNewExpiry] = useState("");
  const [createdToken, setCreatedToken] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [copied, setCopied] = useState(false);

  const load = async () => {
    setLoading(true);
    const data = await listTokens();
    setTokens(data.tokens || []);
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    const data = await createToken(newName.trim(), newRole, newExpiry ? parseInt(newExpiry) : null);
    setCreatedToken(data.token);
    setNewName("");
    setNewExpiry("");
    load();
  };

  const handleDelete = async (id) => {
    await deleteToken(id);
    setConfirmDelete(null);
    load();
  };

  const copyToken = () => {
    navigator.clipboard.writeText(createdToken);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>API Tokens</h2>

        {createdToken && (
          <div className="token-created-banner">
            <p><strong>Token created!</strong> Copy it now — it won't be shown again.</p>
            <div className="token-display">
              <code>{createdToken}</code>
              <button onClick={copyToken} className="btn-small">{copied ? "Copied!" : "Copy"}</button>
            </div>
            <button onClick={() => setCreatedToken(null)} className="btn-small">Dismiss</button>
          </div>
        )}

        <div className="token-create-form">
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Token name (e.g., CI/CD)"
            className="token-input"
          />
          <select value={newRole} onChange={(e) => setNewRole(e.target.value)} className="token-select">
            <option value="viewer">Viewer</option>
            <option value="admin">Admin</option>
          </select>
          <input
            type="number"
            value={newExpiry}
            onChange={(e) => setNewExpiry(e.target.value)}
            placeholder="Expires (days)"
            className="token-input token-input-small"
            min="1"
          />
          <button onClick={handleCreate} className="btn-primary" disabled={!newName.trim()}>Create Token</button>
        </div>

        <p className="muted" style={{ fontSize: 12, margin: "8px 0" }}>
          Use tokens with: <code>Authorization: Bearer sairo_...</code>
        </p>

        {loading ? (
          <div className="empty"><div className="spinner" /></div>
        ) : tokens.length === 0 ? (
          <p className="muted">No API tokens created yet.</p>
        ) : (
          <div style={{ maxHeight: 300, overflowY: "auto" }}>
            <table className="dashboard-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Token</th>
                  <th>Role</th>
                  <th>Created</th>
                  <th>Last Used</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {tokens.map(t => (
                  <tr key={t.id}>
                    <td style={{ fontWeight: 500 }}>{t.name}</td>
                    <td><code style={{ fontSize: 11 }}>{t.token_prefix}</code></td>
                    <td><span className={`role-badge role-${t.role}`}>{t.role}</span></td>
                    <td style={{ fontSize: 12 }}>{formatDate(t.created_at)}</td>
                    <td style={{ fontSize: 12 }}>{t.last_used ? formatDate(t.last_used) : "Never"}</td>
                    <td>
                      <button onClick={() => setConfirmDelete(t)} className="btn-small btn-danger">Revoke</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="modal-actions">
          <button onClick={onClose}>Close</button>
        </div>

        {confirmDelete && (
          <ConfirmDialog
            title="Revoke Token"
            message={`Revoke token "${confirmDelete.name}"? Any integrations using this token will stop working.`}
            confirmLabel="Revoke"
            variant="danger"
            onConfirm={() => handleDelete(confirmDelete.id)}
            onCancel={() => setConfirmDelete(null)}
          />
        )}
      </div>
    </div>
  );
}
