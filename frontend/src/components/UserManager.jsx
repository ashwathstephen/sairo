import React, { useState, useEffect } from "react";
import { listUsers, createUser, updateUserRole, deleteUser, formatDate, getUserPermissions, setUserPermissions, listBuckets, reset2FA } from "../api";
import ConfirmDialog from "./ConfirmDialog";

export default function UserManager({ onClose, currentUser }) {
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newRole, setNewRole] = useState("viewer");
  const [creating, setCreating] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(null);
  // Bucket permissions
  const [expandedUser, setExpandedUser] = useState(null);
  const [allBuckets, setAllBuckets] = useState([]);
  const [userPerms, setUserPerms] = useState({}); // { bucket: "read"|"write" }
  const [permsLoading, setPermsLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const data = await listUsers();
      setUsers(data.users || []);
    } catch {
      setError("Failed to load users");
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  const handleCreate = async () => {
    if (!newUsername.trim() || !newPassword) return;
    setCreating(true);
    setError("");
    try {
      await createUser(newUsername.trim(), newPassword, newRole);
      setNewUsername("");
      setNewPassword("");
      setNewRole("viewer");
      load();
    } catch (err) {
      setError(err.message);
    }
    setCreating(false);
  };

  const handleRoleChange = async (username, role) => {
    setError("");
    try {
      await updateUserRole(username, role);
      setUsers(prev => prev.map(u => u.username === username ? { ...u, role } : u));
      // Collapse permissions if switching to admin
      if (role === "admin" && expandedUser === username) setExpandedUser(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDelete = async (username) => {
    setError("");
    try {
      await deleteUser(username);
      setConfirmDelete(null);
      if (expandedUser === username) setExpandedUser(null);
      load();
    } catch (err) {
      setError(err.message);
      setConfirmDelete(null);
    }
  };

  const isSelf = (username) => currentUser && currentUser.username === username;

  // Expand/collapse bucket permissions for a user
  const togglePermissions = async (username) => {
    if (expandedUser === username) {
      setExpandedUser(null);
      return;
    }
    setExpandedUser(username);
    setPermsLoading(true);
    setError("");
    try {
      const [bucketsData, permsData] = await Promise.all([
        listBuckets(),
        getUserPermissions(username),
      ]);
      setAllBuckets((bucketsData.buckets || []).map(b => b.name));
      const permMap = {};
      for (const p of permsData.permissions || []) {
        permMap[p.bucket] = p.permission;
      }
      setUserPerms(permMap);
    } catch (err) {
      setError(err.message);
    }
    setPermsLoading(false);
  };

  const handlePermChange = async (username, bucket, value) => {
    setError("");
    const updated = { ...userPerms };
    if (value === "none") {
      delete updated[bucket];
    } else {
      updated[bucket] = value;
    }
    setUserPerms(updated);
    // Save all permissions
    try {
      const permissions = Object.entries(updated).map(([b, p]) => ({ bucket: b, permission: p }));
      await setUserPermissions(username, permissions);
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal modal-wide" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>Users</h2>

        {error && <div className="form-error">{error}</div>}

        <div className="user-create-form">
          <input
            type="text"
            value={newUsername}
            onChange={(e) => setNewUsername(e.target.value)}
            placeholder="Username"
            className="user-input"
          />
          <input
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            placeholder="Password (min 8 chars)"
            className="user-input"
          />
          <select value={newRole} onChange={(e) => setNewRole(e.target.value)} className="user-select">
            <option value="viewer">Viewer</option>
            <option value="admin">Admin</option>
          </select>
          <button
            onClick={handleCreate}
            className="btn-primary"
            disabled={!newUsername.trim() || newPassword.length < 8 || creating}
          >
            {creating ? "Creating..." : "Add User"}
          </button>
        </div>

        {loading ? (
          <div className="empty"><div className="spinner" /></div>
        ) : users.length === 0 ? (
          <p className="muted">No users found.</p>
        ) : (
          <div style={{ maxHeight: 500, overflowY: "auto" }}>
            <table className="dashboard-table">
              <thead>
                <tr>
                  <th>Username</th>
                  <th>Role</th>
                  <th>2FA</th>
                  <th>Created</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {users.map(u => (
                  <React.Fragment key={u.username}>
                    <tr
                      className={expandedUser === u.username ? "row-expanded" : ""}
                      style={{ cursor: u.role !== "admin" && !isSelf(u.username) ? "pointer" : undefined }}
                      onClick={() => u.role !== "admin" && !isSelf(u.username) && togglePermissions(u.username)}
                    >
                      <td style={{ fontWeight: 500 }}>
                        {u.username}
                        {isSelf(u.username) && <span className="user-you-badge">you</span>}
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {isSelf(u.username) ? (
                          <span className={`role-badge role-${u.role}`}>{u.role}</span>
                        ) : (
                          <select
                            value={u.role}
                            onChange={(e) => handleRoleChange(u.username, e.target.value)}
                            className="role-select"
                          >
                            <option value="viewer">viewer</option>
                            <option value="admin">admin</option>
                          </select>
                        )}
                      </td>
                      <td onClick={(e) => e.stopPropagation()} style={{ fontSize: 12 }}>
                        {u.totp_enabled ? (
                          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                            <span className="tfa-badge tfa-badge-on">ON</span>
                            {!isSelf(u.username) && (
                              <button onClick={async () => { try { await reset2FA(u.username); load(); } catch (err) { setError(err.message); } }} className="btn-xs">Reset</button>
                            )}
                          </span>
                        ) : (
                          <span className="tfa-badge tfa-badge-off">OFF</span>
                        )}
                      </td>
                      <td style={{ fontSize: 12 }}>{formatDate(u.created_at)}</td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {!isSelf(u.username) && (
                          <button onClick={() => setConfirmDelete(u)} className="btn-small btn-danger">Delete</button>
                        )}
                      </td>
                    </tr>
                    {expandedUser === u.username && u.role !== "admin" && (
                      <tr>
                        <td colSpan={5} style={{ padding: 0 }}>
                          <div className="perm-panel">
                            <div className="perm-header">Bucket Permissions</div>
                            {permsLoading ? (
                              <div className="empty"><div className="spinner" /></div>
                            ) : allBuckets.length === 0 ? (
                              <p className="muted" style={{ margin: "8px 12px" }}>No buckets available.</p>
                            ) : (
                              <div className="perm-grid">
                                {allBuckets.map(bucket => (
                                  <div key={bucket} className="perm-row">
                                    <span className="perm-bucket">{bucket}</span>
                                    <select
                                      value={userPerms[bucket] || "none"}
                                      onChange={(e) => handlePermChange(u.username, bucket, e.target.value)}
                                      className="perm-select"
                                    >
                                      <option value="none">No Access</option>
                                      <option value="read">Read</option>
                                      <option value="write">Write</option>
                                    </select>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <p className="muted" style={{ fontSize: 12, margin: "8px 0 0" }}>
          <strong>Admin</strong> = full access to all buckets. <strong>Viewer</strong> = click a row to assign per-bucket permissions (Read / Write / No Access).
        </p>

        <div className="modal-actions">
          <button onClick={onClose}>Close</button>
        </div>

        {confirmDelete && (
          <ConfirmDialog
            title="Delete User"
            message={`Permanently delete user "${confirmDelete.username}"? They will lose access immediately.`}
            confirmLabel="Delete"
            variant="danger"
            onConfirm={() => handleDelete(confirmDelete.username)}
            onCancel={() => setConfirmDelete(null)}
          />
        )}
      </div>
    </div>
  );
}
