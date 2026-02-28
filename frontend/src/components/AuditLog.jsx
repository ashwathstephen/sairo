import React, { useState, useEffect, useCallback } from "react";
import { getAuditLog, formatDate } from "../api";

const ACTION_CLASSES = {
  upload: "audit-action-upload",
  delete: "audit-action-delete",
  create_folder: "audit-action-create",
  create_bucket: "audit-action-create",
  delete_bucket: "audit-action-delete",
  login: "audit-action-login",
  change_password: "audit-action-config",
  create_user: "audit-action-create",
  delete_user: "audit-action-delete",
  copy: "audit-action-create",
  rename: "audit-action-config",
  abort_multipart: "audit-action-delete",
  config_versioning: "audit-action-config",
  config_lifecycle: "audit-action-config",
  config_cors: "audit-action-config",
  config_policy: "audit-action-config",
  config_acl: "audit-action-config",
  config_tagging: "audit-action-config",
  config_object_acl: "audit-action-config",
  config_object_tagging: "audit-action-config",
};

export default function AuditLog({ onClose }) {
  const [entries, setEntries] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [actionFilter, setActionFilter] = useState("");
  const [userFilter, setUserFilter] = useState("");
  const limit = 50;

  const fetchLog = useCallback(async () => {
    setLoading(true);
    try {
      const params = { limit, offset: page * limit };
      if (actionFilter) params.action = actionFilter;
      if (userFilter) params.username = userFilter;
      const data = await getAuditLog(params);
      setEntries(data.entries || []);
      setTotal(data.total || 0);
    } catch {
      setEntries([]);
    } finally {
      setLoading(false);
    }
  }, [page, actionFilter, userFilter]);

  useEffect(() => { fetchLog(); }, [fetchLog]);

  useEffect(() => {
    const interval = setInterval(fetchLog, 30000);
    return () => clearInterval(interval);
  }, [fetchLog]);

  const totalPages = Math.ceil(total / limit);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal audit-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>Activity Log</h2>

        <div className="audit-filters">
          <select value={actionFilter} onChange={(e) => { setActionFilter(e.target.value); setPage(0); }}>
            <option value="">All actions</option>
            <option value="upload">Upload</option>
            <option value="delete">Delete</option>
            <option value="create_folder">Create Folder</option>
            <option value="create_bucket">Create Bucket</option>
            <option value="delete_bucket">Delete Bucket</option>
            <option value="login">Login</option>
            <option value="change_password">Password Change</option>
            <option value="create_user">Create User</option>
            <option value="delete_user">Delete User</option>
            <option value="copy">Copy</option>
            <option value="rename">Rename/Move</option>
            <option value="config_versioning">Config: Versioning</option>
            <option value="config_lifecycle">Config: Lifecycle</option>
            <option value="config_cors">Config: CORS</option>
            <option value="config_policy">Config: Policy</option>
            <option value="config_acl">Config: ACL</option>
            <option value="config_tagging">Config: Tagging</option>
            <option value="abort_multipart">Abort Multipart</option>
          </select>
          <input
            type="text"
            placeholder="Filter by user..."
            value={userFilter}
            onChange={(e) => { setUserFilter(e.target.value); setPage(0); }}
          />
          <button onClick={fetchLog} style={{ padding: "4px 10px" }}>&#8635;</button>
        </div>

        <div className="audit-scroll">
          {loading ? (
            <div className="empty"><div className="spinner" /> Loading...</div>
          ) : entries.length === 0 ? (
            <div className="empty">No activity recorded yet</div>
          ) : (
            <table className="audit-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>User</th>
                  <th>Action</th>
                  <th>Bucket</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => (
                  <tr key={e.id}>
                    <td style={{ whiteSpace: "nowrap" }}>{formatDate(e.timestamp)}</td>
                    <td>{e.username}</td>
                    <td>
                      <span className={`audit-action-badge ${ACTION_CLASSES[e.action] || "audit-action-config"}`}>
                        {e.action.replace(/_/g, " ")}
                      </span>
                    </td>
                    <td>{e.bucket || "\u2014"}</td>
                    <td title={e.details}>{e.details}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="audit-pagination">
          <span>{total} entr{total === 1 ? "y" : "ies"}</span>
          <div style={{ display: "flex", gap: 6 }}>
            <button disabled={page === 0} onClick={() => setPage(p => p - 1)} style={{ padding: "3px 8px" }}>Prev</button>
            <span style={{ lineHeight: "28px" }}>Page {page + 1} of {totalPages || 1}</span>
            <button disabled={page >= totalPages - 1} onClick={() => setPage(p => p + 1)} style={{ padding: "3px 8px" }}>Next</button>
          </div>
        </div>

        <div className="modal-actions">
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
