import React, { useState, useEffect } from "react";
import ConfirmDialog from "./ConfirmDialog";
import { formatSize, formatDate, getObjectInfo, getObjectVersions, getPresignedUrl, getObjectTagging, putObjectTagging, getObjectAcl, putObjectAcl, versionRestore, versionDelete, getVersionPresignedUrl, createShareLink } from "../api";

export default function ObjectInfo({ bucket, fileKey, onClose, role }) {
  const isAdmin = role === "admin";
  const [info, setInfo] = useState(null);
  const [versions, setVersions] = useState(null);
  const [tags, setTags] = useState(null);
  const [acl, setAcl] = useState(null);
  const [presignedUrl, setPresignedUrl] = useState(null);
  const [loading, setLoading] = useState(true);
  const [copySuccess, setCopySuccess] = useState(false);
  const [activeTab, setActiveTab] = useState("details");
  const [newTagKey, setNewTagKey] = useState("");
  const [newTagValue, setNewTagValue] = useState("");
  const [objectAclValue, setObjectAclValue] = useState("private");
  const [versionAction, setVersionAction] = useState(null); // {type, versionId}
  const [confirmDeleteVersion, setConfirmDeleteVersion] = useState(null); // versionId
  const [alertMessage, setAlertMessage] = useState(null);
  const [shareExpiry, setShareExpiry] = useState("168");
  const [shareMaxDl, setShareMaxDl] = useState("");
  const [sharePassword, setSharePassword] = useState("");
  const [shareLink, setShareLink] = useState(null);
  const [shareCopied, setShareCopied] = useState(false);
  const [shareCreating, setShareCreating] = useState(false);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      getObjectInfo(bucket, fileKey),
      getObjectVersions(bucket, fileKey),
      getObjectTagging(bucket, fileKey).catch(() => ({ tags: {} })),
      getObjectAcl(bucket, fileKey).catch(() => ({ owner: {}, grants: [] })),
    ]).then(([infoData, versionData, tagData, aclData]) => {
      setInfo(infoData);
      setVersions(versionData);
      setTags(tagData);
      setAcl(aclData);
      const hasPublicRead = aclData.grants.some(g => g.grantee?.URI?.includes("AllUsers") && g.permission === "READ");
      const hasPublicWrite = aclData.grants.some(g => g.grantee?.URI?.includes("AllUsers") && g.permission === "WRITE");
      const hasAuthRead = aclData.grants.some(g => g.grantee?.URI?.includes("AuthenticatedUsers"));
      if (hasPublicRead && hasPublicWrite) setObjectAclValue("public-read-write");
      else if (hasPublicRead) setObjectAclValue("public-read");
      else if (hasAuthRead) setObjectAclValue("authenticated-read");
      else setObjectAclValue("private");
      setLoading(false);
    }).catch(e => {
      setAlertMessage("Failed to load object info: " + (e.message || "Unknown error"));
      setLoading(false);
    });
  }, [fileKey, bucket]);

  const refreshVersions = async () => {
    const versionData = await getObjectVersions(bucket, fileKey);
    setVersions(versionData);
  };

  const generatePresignedUrl = async (hours) => {
    try {
      const data = await getPresignedUrl(bucket, fileKey, hours * 3600);
      setPresignedUrl(data.url);
    } catch (e) {
      setAlertMessage("Failed to generate URL: " + (e.message || "Unknown error"));
    }
  };

  const copyUrl = () => {
    navigator.clipboard.writeText(presignedUrl);
    setCopySuccess(true);
    setTimeout(() => setCopySuccess(false), 2000);
  };

  const addTag = async () => {
    if (!newTagKey.trim()) return;
    const updated = { ...tags.tags, [newTagKey.trim()]: newTagValue.trim() };
    try {
      await putObjectTagging(bucket, fileKey, updated);
      setTags({ tags: updated });
      setNewTagKey("");
      setNewTagValue("");
    } catch (e) {
      setAlertMessage("Failed to save tag: " + (e.message || "Unknown error"));
    }
  };

  const removeTag = async (key) => {
    const updated = { ...tags.tags };
    delete updated[key];
    try {
      await putObjectTagging(bucket, fileKey, updated);
      setTags({ tags: updated });
    } catch (e) {
      setAlertMessage("Failed to remove tag: " + (e.message || "Unknown error"));
    }
  };

  const handleVersionDownload = async (versionId) => {
    try {
      const data = await getVersionPresignedUrl(bucket, fileKey, versionId);
      window.open(data.url, "_blank");
    } catch (e) {
      setAlertMessage("Failed to download version: " + (e.message || "Unknown error"));
    }
  };

  const handleVersionRestore = async (versionId) => {
    setVersionAction({ type: "restoring", versionId });
    try {
      await versionRestore(bucket, fileKey, versionId);
      await refreshVersions();
      const newInfo = await getObjectInfo(bucket, fileKey);
      setInfo(newInfo);
      setVersionAction(null);
    } catch (e) {
      setVersionAction({ type: "error", versionId, message: e.message });
      setTimeout(() => setVersionAction(null), 3000);
    }
  };

  const handleVersionDelete = async (versionId) => {
    setVersionAction({ type: "deleting", versionId });
    try {
      await versionDelete(bucket, fileKey, versionId);
      await refreshVersions();
      setVersionAction(null);
    } catch (e) {
      setVersionAction({ type: "error", versionId, message: e.message });
      setTimeout(() => setVersionAction(null), 3000);
    }
  };

  const allVersionEntries = versions ? [
    ...versions.versions.map(v => ({ ...v, type: "version" })),
    ...(versions.delete_markers || []).map(d => ({ ...d, type: "delete_marker" })),
  ].sort((a, b) => new Date(b.last_modified) - new Date(a.last_modified)) : [];

  const tabs = [
    { id: "details", label: "Details" },
    { id: "tags", label: "Tags" },
    { id: "versions", label: `Versions${allVersionEntries.length > 1 ? ` (${allVersionEntries.length})` : ""}` },
    { id: "acl", label: "ACL" },
    { id: "share", label: "Share" },
  ];

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal modal-wide" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>Object Details</h2>
        {loading ? (
          <div className="empty"><div className="spinner" /> Loading...</div>
        ) : info && (
          <>
            <div className="tab-bar">
              {tabs.map((t) => (
                <button key={t.id} className={`tab-btn ${activeTab === t.id ? "tab-active" : ""}`} onClick={() => setActiveTab(t.id)}>
                  {t.label}
                </button>
              ))}
            </div>

            {activeTab === "details" && (
              <table className="info-table">
                <tbody>
                  <tr><td className="info-label">Key</td><td className="info-value mono">{info.key}</td></tr>
                  <tr><td className="info-label">Size</td><td className="info-value">{formatSize(info.size)}</td></tr>
                  <tr><td className="info-label">Content Type</td><td className="info-value">{info.content_type || "\u2014"}</td></tr>
                  <tr><td className="info-label">ETag</td><td className="info-value mono">{info.etag}</td></tr>
                  <tr><td className="info-label">Last Modified</td><td className="info-value">{formatDate(info.last_modified)}</td></tr>
                  <tr><td className="info-label">Storage Class</td><td className="info-value">{info.storage_class}</td></tr>
                  {info.version_id && (
                    <tr><td className="info-label">Version ID</td><td className="info-value mono">{info.version_id}</td></tr>
                  )}
                </tbody>
              </table>
            )}

            {activeTab === "tags" && tags && (
              <div>
                {Object.keys(tags.tags).length === 0 ? (
                  <p className="muted" style={{ padding: "12px 0" }}>No tags on this object</p>
                ) : (
                  <table className="info-table">
                    <thead><tr><th>Key</th><th>Value</th><th>Action</th></tr></thead>
                    <tbody>
                      {Object.entries(tags.tags).map(([k, v]) => (
                        <tr key={k}>
                          <td className="mono">{k}</td>
                          <td>{v}</td>
                          <td>{isAdmin && <button className="btn-danger btn-xs" onClick={() => removeTag(k)}>Remove</button>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {isAdmin && <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                  <input type="text" placeholder="Key" value={newTagKey} onChange={(e) => setNewTagKey(e.target.value)} className="filter-input" style={{ width: 120 }} />
                  <input type="text" placeholder="Value" value={newTagValue} onChange={(e) => setNewTagValue(e.target.value)} className="filter-input" style={{ width: 120 }} />
                  <button onClick={addTag} className="btn-primary btn-xs">Add Tag</button>
                </div>}
              </div>
            )}

            {activeTab === "versions" && versions && (
              <div>
                {allVersionEntries.length <= 1 && !versions.delete_markers?.length ? (
                  <p className="muted" style={{ padding: "12px 0" }}>No previous versions</p>
                ) : (
                  <div className="version-browser">
                    <table className="version-table">
                      <thead>
                        <tr>
                          <th>Version</th>
                          <th>Size</th>
                          <th>Modified</th>
                          <th>ID</th>
                          <th>Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {allVersionEntries.map((v, i) => {
                          const isDelMarker = v.type === "delete_marker";
                          const isBusy = versionAction && versionAction.versionId === v.version_id && (versionAction.type === "restoring" || versionAction.type === "deleting");
                          const hasError = versionAction && versionAction.versionId === v.version_id && versionAction.type === "error";
                          return (
                            <tr key={v.version_id || i} className={`${v.is_latest ? "version-row-latest" : ""} ${isDelMarker ? "version-row-deleted" : ""}`}>
                              <td>
                                {v.is_latest && <span className="version-badge-latest">Current</span>}
                                {!v.is_latest && !isDelMarker && <span className="version-badge-old">v{allVersionEntries.length - i}</span>}
                                {isDelMarker && <span className="version-badge-deleted">Deleted</span>}
                              </td>
                              <td>{isDelMarker ? "\u2014" : formatSize(v.size)}</td>
                              <td>{formatDate(v.last_modified)}</td>
                              <td className="mono" style={{ fontSize: 11 }} title={v.version_id}>{(v.version_id || "").slice(0, 16)}</td>
                              <td className="version-actions">
                                {isBusy && <span className="version-busy">{versionAction.type}...</span>}
                                {hasError && <span className="version-error">{versionAction.message}</span>}
                                {!isBusy && !hasError && !isDelMarker && (
                                  <>
                                    <button className="btn-small" onClick={() => handleVersionDownload(v.version_id)} title="Download this version">&#8595;</button>
                                    {!v.is_latest && isAdmin && (
                                      <button className="btn-small btn-primary" onClick={() => handleVersionRestore(v.version_id)} title="Restore this version as current">Restore</button>
                                    )}
                                    {!v.is_latest && isAdmin && (
                                      <button className="btn-small btn-danger" onClick={() => setConfirmDeleteVersion(v.version_id)} title="Permanently delete this version">Delete</button>
                                    )}
                                  </>
                                )}
                                {!isBusy && !hasError && isDelMarker && isAdmin && (
                                  <button className="btn-small btn-danger" onClick={() => setConfirmDeleteVersion(v.version_id)} title="Remove delete marker">Remove</button>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

            {activeTab === "acl" && acl && (
              <div>
                {isAdmin && <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
                  <label style={{ fontSize: 13, fontWeight: 600 }}>Canned ACL:</label>
                  <select
                    value={objectAclValue}
                    onChange={(e) => setObjectAclValue(e.target.value)}
                    style={{ padding: "6px 10px", border: "1px solid var(--border-input)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text)" }}
                  >
                    <option value="private">private</option>
                    <option value="public-read">public-read</option>
                    <option value="public-read-write">public-read-write</option>
                    <option value="authenticated-read">authenticated-read</option>
                  </select>
                  <button className="btn-primary btn-xs" onClick={async () => {
                    const result = await putObjectAcl(bucket, fileKey, objectAclValue);
                    if (result.supported === false) {
                      setAlertMessage(result.error || "ACL modification not supported");
                      return;
                    }
                    const newAcl = await getObjectAcl(bucket, fileKey).catch(() => ({ owner: {}, grants: [] }));
                    setAcl(newAcl);
                  }}>Apply</button>
                </div>}
                <p style={{ fontSize: 13, marginBottom: 8 }}><strong>Owner:</strong> {acl.owner?.DisplayName || acl.owner?.ID || "\u2014"}</p>
                {acl.grants.length === 0 ? (
                  <p className="muted">No ACL grants</p>
                ) : (
                  <table className="info-table">
                    <thead><tr><th>Grantee</th><th>Type</th><th>Permission</th></tr></thead>
                    <tbody>
                      {acl.grants.map((g, i) => (
                        <tr key={i}>
                          <td className="mono">{g.grantee?.DisplayName || g.grantee?.ID || g.grantee?.URI || "\u2014"}</td>
                          <td>{g.grantee?.Type || "\u2014"}</td>
                          <td>{g.permission}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            {activeTab === "share" && (
              <div>
                <h3>Presigned URL</h3>
                <div className="presigned-actions">
                  <button onClick={() => generatePresignedUrl(1)}>1 hour</button>
                  <button onClick={() => generatePresignedUrl(24)}>24 hours</button>
                  <button onClick={() => generatePresignedUrl(168)}>7 days</button>
                </div>
                {presignedUrl && (
                  <div className="presigned-url">
                    <input type="text" value={presignedUrl} readOnly className="url-input" />
                    <button onClick={copyUrl} className="btn-primary">
                      {copySuccess ? "Copied!" : "Copy"}
                    </button>
                  </div>
                )}

                <hr style={{ border: "none", borderTop: "1px solid var(--border-light)", margin: "16px 0" }} />

                <h3>Share Link</h3>
                <p className="muted" style={{ fontSize: 12, margin: "4px 0 10px" }}>Create a shareable link with optional password protection and download limits.</p>
                <div className="share-link-form">
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                    <label style={{ fontSize: 12, fontWeight: 600 }}>Expires in:</label>
                    <select value={shareExpiry} onChange={(e) => setShareExpiry(e.target.value)} style={{ padding: "4px 8px", fontSize: 12, border: "1px solid var(--border-input)", borderRadius: 4, background: "var(--bg-input)", color: "var(--text)" }}>
                      <option value="1">1 hour</option>
                      <option value="24">24 hours</option>
                      <option value="168">7 days</option>
                      <option value="720">30 days</option>
                    </select>
                    <input type="number" placeholder="Max downloads" value={shareMaxDl} onChange={(e) => setShareMaxDl(e.target.value)} min="1" style={{ width: 120, padding: "4px 8px", fontSize: 12, border: "1px solid var(--border-input)", borderRadius: 4, background: "var(--bg-input)", color: "var(--text)" }} />
                    <input type="text" placeholder="Password (optional)" value={sharePassword} onChange={(e) => setSharePassword(e.target.value)} style={{ width: 140, padding: "4px 8px", fontSize: 12, border: "1px solid var(--border-input)", borderRadius: 4, background: "var(--bg-input)", color: "var(--text)" }} />
                    <button className="btn-primary btn-xs" disabled={shareCreating} onClick={async () => {
                      setShareCreating(true);
                      try {
                        const result = await createShareLink(bucket, fileKey, parseInt(shareExpiry), shareMaxDl ? parseInt(shareMaxDl) : null, sharePassword || null);
                        setShareLink(window.location.origin + "/share/" + result.token);
                      } catch (e) {
                        setAlertMessage(e.message);
                      }
                      setShareCreating(false);
                    }}>{shareCreating ? "Creating..." : "Create Link"}</button>
                  </div>
                  {shareLink && (
                    <div className="presigned-url" style={{ marginTop: 10 }}>
                      <input type="text" value={shareLink} readOnly className="url-input" />
                      <button onClick={() => { navigator.clipboard.writeText(shareLink); setShareCopied(true); setTimeout(() => setShareCopied(false), 2000); }} className="btn-primary">
                        {shareCopied ? "Copied!" : "Copy"}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            )}
          </>
        )}
        <div className="modal-actions">
          <button onClick={onClose}>Close</button>
        </div>
        {confirmDeleteVersion && (
          <ConfirmDialog
            title="Delete Version"
            message={<>
              <p>Delete this version permanently?</p>
              <p className="mono" style={{ fontSize: 12, wordBreak: "break-all" }}>Version: {confirmDeleteVersion.slice(0, 24)}...</p>
              <p className="warning">This cannot be undone.</p>
            </>}
            confirmLabel="Delete"
            variant="danger"
            onConfirm={() => { const vid = confirmDeleteVersion; setConfirmDeleteVersion(null); handleVersionDelete(vid); }}
            onCancel={() => setConfirmDeleteVersion(null)}
          />
        )}
        {alertMessage && (
          <ConfirmDialog
            title="Error"
            message={alertMessage}
            hideCancel
            onConfirm={() => setAlertMessage(null)}
            onCancel={() => setAlertMessage(null)}
          />
        )}
      </div>
    </div>
  );
}
