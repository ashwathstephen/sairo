import React, { useState, useEffect } from "react";
import { listUsers, createUser, updateUserRole, deleteUser, formatDate, getUserPermissions, setUserPermissions, listBuckets, reset2FA } from "../api";
import ConfirmDialog from "./ConfirmDialog";

// How each account authenticates — shown as a badge so admins can tell local
// accounts apart from SSO/LDAP ones at a glance.
const SOURCE_META = {
  local: { label: "Local", cls: "src-local" },
  oidc: { label: "SSO", cls: "src-sso" },
  oauth: { label: "OAuth", cls: "src-oauth" },
  oauth_google: { label: "Google", cls: "src-oauth" },
  oauth_github: { label: "GitHub", cls: "src-oauth" },
  ldap: { label: "LDAP", cls: "src-ldap" },
};
const sourceMeta = (s) => SOURCE_META[s] || SOURCE_META.local;

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
  const [permSearch, setPermSearch] = useState("");
  const [savedFlash, setSavedFlash] = useState(false);

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
    setPermSearch("");
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

  // Persist the whole permission map, then reflect the new grant count on the
  // row and flash a "Saved" indicator.
  const persistPerms = async (username, nextMap) => {
    setUserPerms(nextMap);
    try {
      const permissions = Object.entries(nextMap).map(([b, p]) => ({ bucket: b, permission: p }));
      await setUserPermissions(username, permissions);
      setUsers(prev => prev.map(u => u.username === username ? { ...u, bucket_count: permissions.length } : u));
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 1200);
    } catch (err) {
      setError(err.message);
    }
  };

  const handlePermChange = (username, bucket, value) => {
    setError("");
    const updated = { ...userPerms };
    if (value === "none") delete updated[bucket];
    else updated[bucket] = value;
    persistPerms(username, updated);
  };

  // Quick actions. "Grant read to all" keeps any existing write grants.
  const grantReadAll = (username) => {
    const m = {};
    allBuckets.forEach(b => { m[b] = userPerms[b] === "write" ? "write" : "read"; });
    persistPerms(username, m);
  };
  const revokeAll = (username) => persistPerms(username, {});

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
                  <th>Access</th>
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
                        <span className={`src-badge ${sourceMeta(u.auth_source).cls}`}>{sourceMeta(u.auth_source).label}</span>
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
                      <td onClick={(e) => e.stopPropagation()} style={{ fontSize: 12 }}>
                        {u.role === "admin" ? (
                          <span className="muted">All buckets</span>
                        ) : (
                          <button
                            className={`access-pill ${expandedUser === u.username ? "access-pill-open" : ""}`}
                            onClick={() => !isSelf(u.username) && togglePermissions(u.username)}
                            disabled={isSelf(u.username)}
                            title="Manage bucket access"
                          >
                            {(u.bucket_count || 0)} bucket{(u.bucket_count || 0) === 1 ? "" : "s"}
                          </button>
                        )}
                      </td>
                      <td style={{ fontSize: 12 }}>{formatDate(u.created_at)}</td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <div className="user-row-actions">
                          {u.role !== "admin" && !isSelf(u.username) && (
                            <button onClick={() => togglePermissions(u.username)} className="btn-small">
                              {expandedUser === u.username ? "Done" : "Manage access"}
                            </button>
                          )}
                          {!isSelf(u.username) && (
                            <button onClick={() => setConfirmDelete(u)} className="btn-small btn-danger">Delete</button>
                          )}
                        </div>
                      </td>
                    </tr>
                    {expandedUser === u.username && u.role !== "admin" && (
                      <tr>
                        <td colSpan={6} style={{ padding: 0 }}>
                          <div className="perm-panel">
                            <div className="perm-panel-head">
                              <strong>Bucket access for {u.username}</strong>
                              <span className={`perm-saved ${savedFlash ? "perm-saved-on" : ""}`}>Saved ✓</span>
                              <div className="perm-quick">
                                <button className="btn-xs" onClick={() => grantReadAll(u.username)} disabled={allBuckets.length === 0}>Grant read to all</button>
                                <button className="btn-xs" onClick={() => revokeAll(u.username)} disabled={Object.keys(userPerms).length === 0}>Revoke all</button>
                              </div>
                            </div>
                            {permsLoading ? (
                              <div className="empty"><div className="spinner" /></div>
                            ) : allBuckets.length === 0 ? (
                              <p className="muted" style={{ margin: "8px 12px" }}>No buckets available to grant.</p>
                            ) : (
                              <>
                                {allBuckets.length > 6 && (
                                  <input
                                    className="perm-search"
                                    placeholder="Filter buckets…"
                                    value={permSearch}
                                    onChange={(e) => setPermSearch(e.target.value)}
                                  />
                                )}
                                <div className="perm-grid">
                                  {allBuckets
                                    .filter(b => b.toLowerCase().includes(permSearch.toLowerCase()))
                                    .map(bucket => {
                                      const cur = userPerms[bucket] || "none";
                                      return (
                                        <div key={bucket} className="perm-row">
                                          <span className="perm-bucket">{bucket}</span>
                                          <div className="perm-seg" role="group" aria-label={`Access for ${bucket}`}>
                                            {["none", "read", "write"].map(level => (
                                              <button
                                                key={level}
                                                className={`perm-seg-btn ${cur === level ? "perm-seg-active perm-seg-" + level : ""}`}
                                                onClick={() => handlePermChange(u.username, bucket, level)}
                                              >
                                                {level === "none" ? "No access" : level === "read" ? "Read" : "Write"}
                                              </button>
                                            ))}
                                          </div>
                                        </div>
                                      );
                                    })}
                                </div>
                              </>
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
          <strong>Admin</strong> = full access to all buckets. <strong>Viewer</strong> = use <strong>Manage access</strong> to grant specific buckets (Read / Write). SSO &amp; LDAP users sign in through your identity provider; you assign their bucket access here.
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
