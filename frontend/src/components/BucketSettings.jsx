import React, { useState, useEffect } from "react";
import ConfirmDialog from "./ConfirmDialog";
import {
  getCrawlStatus, triggerCrawl, formatSize, getVersioning, putVersioning,
  getLifecycle, putLifecycle, deleteLifecycle,
  getCors, putCors, deleteCors,
  getBucketPolicy, putBucketPolicy, deleteBucketPolicy,
  getBucketAcl, putBucketAcl, getBucketTagging, putBucketTagging,
  getMultipartUploads, abortMultipart, abortAllMultipart, getObjectLock,
} from "../api";

export default function BucketSettings({ bucket, onClose, role }) {
  const isAdmin = role === "admin";
  const [versioning, setVersioning] = useState(null);
  const [lifecycle, setLifecycle] = useState(null);
  const [cors, setCors] = useState(null);
  const [policy, setPolicy] = useState(null);
  const [acl, setAcl] = useState(null);
  const [tags, setTags] = useState(null);
  const [multipart, setMultipart] = useState(null);
  const [objectLock, setObjectLock] = useState(null);
  const [crawl, setCrawl] = useState(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("overview");
  const [policyText, setPolicyText] = useState("");
  const [corsText, setCorsText] = useState("");
  const [newTagKey, setNewTagKey] = useState("");
  const [newTagValue, setNewTagValue] = useState("");
  const [lifecycleRules, setLifecycleRules] = useState([]);
  const [lifecycleDirty, setLifecycleDirty] = useState(false);
  const [bucketAclValue, setBucketAclValue] = useState("private");
  const [alertMessage, setAlertMessage] = useState(null);

  useEffect(() => {
    Promise.all([
      getVersioning(bucket),
      getLifecycle(bucket),
      getCors(bucket),
      getBucketPolicy(bucket).catch(() => ({ policy: null })),
      getBucketAcl(bucket).catch(() => ({ owner: {}, grants: [] })),
      getBucketTagging(bucket).catch(() => ({ tags: {} })),
      getObjectLock(bucket).catch(() => ({ enabled: false, rule: {} })),
      getCrawlStatus(bucket),
    ]).then(([v, l, c, p, a, t, ol, cr]) => {
      setVersioning(v);
      setLifecycle(l);
      setLifecycleRules((l.rules || []).map(r => ({ ...r })));
      setCors(c);
      setCorsText(c.cors_rules.length > 0 ? JSON.stringify(c.cors_rules, null, 2) : "[]");
      setPolicy(p);
      setPolicyText(p.policy ? JSON.stringify(p.policy, null, 2) : "");
      setAcl(a);
      setTags(t);
      setObjectLock(ol);
      setCrawl(cr);
      // Detect canned ACL
      const hasPublicRead = a.grants.some(g => g.grantee?.URI?.includes("AllUsers") && g.permission === "READ");
      const hasPublicWrite = a.grants.some(g => g.grantee?.URI?.includes("AllUsers") && g.permission === "WRITE");
      const hasAuthRead = a.grants.some(g => g.grantee?.URI?.includes("AuthenticatedUsers"));
      if (hasPublicRead && hasPublicWrite) setBucketAclValue("public-read-write");
      else if (hasPublicRead) setBucketAclValue("public-read");
      else if (hasAuthRead) setBucketAclValue("authenticated-read");
      else setBucketAclValue("private");
      setLoading(false);
    }).catch(e => {
      setAlertMessage("Failed to load bucket settings: " + (e.message || "Unknown error"));
      setLoading(false);
    });
  }, [bucket]);

  // Lazy-load multipart data only when tab is clicked
  const [multipartLoading, setMultipartLoading] = useState(false);
  useEffect(() => {
    if (activeTab === "multipart" && !multipart && !multipartLoading) {
      setMultipartLoading(true);
      getMultipartUploads(bucket, true).then(m => {
        setMultipart(m);
        setMultipartLoading(false);
      }).catch(e => {
        setMultipartLoading(false);
        setAlertMessage("Failed to load multipart uploads: " + (e.message || "Unknown error"));
      });
    }
  }, [activeTab]);

  const [confirmAbortAll, setConfirmAbortAll] = useState(false);
  const [aborting, setAborting] = useState(false);

  const handleAbortUpload = async (key, uploadId) => {
    try {
      await abortMultipart(bucket, key, uploadId);
      const res = await getMultipartUploads(bucket, true);
      setMultipart(res);
    } catch (e) {
      setAlertMessage("Failed to abort upload: " + (e.message || "Unknown error"));
    }
  };

  const handleAbortAll = async () => {
    setAborting(true);
    try {
      await abortAllMultipart(bucket);
      const res = await getMultipartUploads(bucket, true);
      setMultipart(res);
    } catch (e) {
      setAlertMessage("Failed to abort uploads: " + (e.message || "Unknown error"));
    } finally {
      setAborting(false);
      setConfirmAbortAll(false);
    }
  };

  const handleRecrawl = async () => {
    try {
      await triggerCrawl(bucket);
      setCrawl(await getCrawlStatus(bucket));
    } catch (e) {
      setAlertMessage("Failed to trigger re-index: " + (e.message || "Unknown error"));
    }
  };

  const handleSavePolicy = async () => {
    try {
      const parsed = JSON.parse(policyText);
      await putBucketPolicy(bucket, parsed);
      setPolicy({ policy: parsed });
    } catch (e) {
      setAlertMessage("Invalid JSON: " + e.message);
    }
  };

  const handleDeletePolicy = async () => {
    try {
      await deleteBucketPolicy(bucket);
      setPolicy({ policy: null });
      setPolicyText("");
    } catch (e) {
      setAlertMessage("Failed to delete policy: " + (e.message || "Unknown error"));
    }
  };

  const handleSaveCors = async () => {
    try {
      const parsed = JSON.parse(corsText);
      await putCors(bucket, parsed);
      setCors({ cors_rules: parsed });
    } catch (e) {
      setAlertMessage("Invalid JSON: " + e.message);
    }
  };

  const handleDeleteCors = async () => {
    try {
      await deleteCors(bucket);
      setCors({ cors_rules: [] });
      setCorsText("[]");
    } catch (e) {
      setAlertMessage("Failed to delete CORS: " + (e.message || "Unknown error"));
    }
  };

  const addTag = async () => {
    if (!newTagKey.trim()) return;
    const updated = { ...tags.tags, [newTagKey.trim()]: newTagValue.trim() };
    try {
      await putBucketTagging(bucket, updated);
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
      await putBucketTagging(bucket, updated);
      setTags({ tags: updated });
    } catch (e) {
      setAlertMessage("Failed to remove tag: " + (e.message || "Unknown error"));
    }
  };

  const updateLifecycleRule = (index, field, value) => {
    setLifecycleRules(prev => prev.map((r, i) => i === index ? { ...r, [field]: value } : r));
    setLifecycleDirty(true);
  };

  const addLifecycleRule = () => {
    setLifecycleRules(prev => [...prev, {
      id: `rule-${prev.length + 1}`,
      prefix: "",
      status: "Enabled",
      expiration_days: null,
      noncurrent_days: null,
      abort_days: null,
    }]);
    setLifecycleDirty(true);
  };

  const removeLifecycleRule = (index) => {
    setLifecycleRules(prev => prev.filter((_, i) => i !== index));
    setLifecycleDirty(true);
  };

  const saveLifecycleRules = async () => {
    try {
      const cleaned = lifecycleRules.map(r => ({
        id: r.id || "rule",
        prefix: r.prefix || "",
        status: r.status || "Enabled",
        expiration_days: r.expiration_days ? parseInt(r.expiration_days) : null,
        noncurrent_days: r.noncurrent_days ? parseInt(r.noncurrent_days) : null,
        abort_days: r.abort_days ? parseInt(r.abort_days) : null,
      }));
      if (cleaned.length === 0) {
        await deleteLifecycle(bucket);
      } else {
        await putLifecycle(bucket, cleaned);
      }
      setLifecycle({ rules: cleaned });
      setLifecycleDirty(false);
    } catch (e) {
      setAlertMessage("Failed to save lifecycle rules: " + e.message);
    }
  };

  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "lifecycle", label: "Lifecycle" },
    { id: "policy", label: "Policy" },
    { id: "acl", label: "ACL" },
    { id: "tags", label: "Tags" },
    { id: "cors", label: "CORS" },
    { id: "multipart", label: "Multipart" },
    { id: "index", label: "Index" },
  ];

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal modal-wide" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h2>Bucket: {bucket}</h2>
        {loading ? (
          <div className="empty"><div className="spinner" /> Loading...</div>
        ) : (
          <>
            <div className="tab-bar">
              {tabs.map((t) => (
                <button key={t.id} className={`tab-btn ${activeTab === t.id ? "tab-active" : ""}`} onClick={() => setActiveTab(t.id)}>
                  {t.label}
                </button>
              ))}
            </div>

            {activeTab === "overview" && (
              <div className="settings-sections">
                <section>
                  <h3>Versioning</h3>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <span className={`status-badge ${versioning.status === "Enabled" ? "status-on" : "status-off"}`}>
                      {versioning.status}
                    </span>
                    {isAdmin && <button
                      onClick={async () => {
                        const enable = versioning.status !== "Enabled";
                        await putVersioning(bucket, enable);
                        setVersioning({ ...versioning, status: enable ? "Enabled" : "Suspended" });
                      }}
                      className={versioning.status === "Enabled" ? "btn-danger btn-xs" : "btn-primary btn-xs"}
                    >
                      {versioning.status === "Enabled" ? "Suspend" : "Enable"}
                    </button>}
                  </div>
                </section>
                <section>
                  <h3>Object Lock</h3>
                  <span className={`status-badge ${objectLock.enabled ? "status-on" : "status-off"}`}>
                    {objectLock.enabled ? "Enabled" : "Disabled"}
                  </span>
                  {objectLock.supported === false && <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>(not supported)</span>}
                </section>
              </div>
            )}

            {activeTab === "lifecycle" && (
              <div>
                <h3>Lifecycle Rules</h3>

                {/* Summary banner */}
                {lifecycleRules.some(r => r.status === "Enabled" && (r.expiration_days || r.noncurrent_days)) && (
                  <div className="lc-warning">
                    Warning: Active rules will permanently delete objects matching their criteria.
                  </div>
                )}

                {lifecycleRules.length > 0 && (
                  <div className="lc-summary">
                    {lifecycleRules.filter(r => r.status === "Enabled").map(r => {
                      const parts = [];
                      if (r.expiration_days) parts.push(`expire after ${r.expiration_days} days`);
                      if (r.noncurrent_days) parts.push(`old versions after ${r.noncurrent_days} days`);
                      if (r.abort_days) parts.push(`abort uploads after ${r.abort_days} days`);
                      if (parts.length === 0) return null;
                      const scope = r.prefix ? `"${r.prefix}"` : "all objects";
                      return <div key={r.id}>{scope}: {parts.join(", ")}</div>;
                    })}
                  </div>
                )}

                {lifecycleRules.length === 0 ? (
                  <p className="muted">No lifecycle rules configured. Add a rule to automatically manage object expiration.</p>
                ) : (
                  <div className="lc-rules">
                    {lifecycleRules.map((r, i) => (
                      <div key={i} className={`lc-card ${r.status !== "Enabled" ? "lc-card-disabled" : ""}`}>
                        <div className="lc-card-header">
                          <input
                            className="lc-name-input"
                            value={r.id}
                            onChange={(e) => updateLifecycleRule(i, "id", e.target.value)}
                            placeholder="Rule name"
                            readOnly={!isAdmin}
                          />
                          <div className="lc-card-actions">
                            {isAdmin && (
                              <label className="lc-toggle">
                                <input
                                  type="checkbox"
                                  checked={r.status === "Enabled"}
                                  onChange={(e) => updateLifecycleRule(i, "status", e.target.checked ? "Enabled" : "Disabled")}
                                />
                                <span className="lc-toggle-label">{r.status === "Enabled" ? "Active" : "Disabled"}</span>
                              </label>
                            )}
                            {!isAdmin && <span className={`status-badge ${r.status === "Enabled" ? "status-on" : "status-off"}`}>{r.status}</span>}
                            {isAdmin && <button className="btn-danger btn-xs" onClick={() => removeLifecycleRule(i)}>Remove</button>}
                          </div>
                        </div>

                        <div className="lc-card-body">
                          <div className="lc-field">
                            <span className="lc-label">Apply to</span>
                            <input
                              className="lc-input"
                              value={r.prefix}
                              onChange={(e) => updateLifecycleRule(i, "prefix", e.target.value)}
                              placeholder="All objects (or enter prefix like images/)"
                              readOnly={!isAdmin}
                            />
                          </div>

                          <div className="lc-field">
                            <span className="lc-label">Delete objects after</span>
                            <div className="lc-days-input">
                              <input
                                type="number"
                                min="1"
                                className="lc-input lc-input-sm"
                                value={r.expiration_days || ""}
                                onChange={(e) => updateLifecycleRule(i, "expiration_days", e.target.value ? parseInt(e.target.value) : null)}
                                placeholder="—"
                                readOnly={!isAdmin}
                              />
                              <span className="lc-unit">days</span>
                            </div>
                          </div>

                          <div className="lc-field">
                            <span className="lc-label">Delete old versions after</span>
                            <div className="lc-days-input">
                              <input
                                type="number"
                                min="1"
                                className="lc-input lc-input-sm"
                                value={r.noncurrent_days || ""}
                                onChange={(e) => updateLifecycleRule(i, "noncurrent_days", e.target.value ? parseInt(e.target.value) : null)}
                                placeholder="—"
                                readOnly={!isAdmin}
                              />
                              <span className="lc-unit">days</span>
                            </div>
                          </div>

                          <div className="lc-field">
                            <span className="lc-label">Abort incomplete uploads after</span>
                            <div className="lc-days-input">
                              <input
                                type="number"
                                min="1"
                                className="lc-input lc-input-sm"
                                value={r.abort_days || ""}
                                onChange={(e) => updateLifecycleRule(i, "abort_days", e.target.value ? parseInt(e.target.value) : null)}
                                placeholder="—"
                                readOnly={!isAdmin}
                              />
                              <span className="lc-unit">days</span>
                            </div>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {isAdmin && (
                  <div className="lc-actions">
                    <button className="btn-settings" onClick={addLifecycleRule}>+ Add Rule</button>
                    {lifecycleDirty && <button className="btn-primary" onClick={saveLifecycleRules}>Save Changes</button>}
                  </div>
                )}
              </div>
            )}

            {activeTab === "policy" && (
              <div>
                <h3>Bucket Policy</h3>
                <textarea
                  className="code-block"
                  style={{ width: "100%", minHeight: 200, fontFamily: "monospace", resize: "vertical" }}
                  value={policyText}
                  onChange={(e) => setPolicyText(e.target.value)}
                  placeholder='{"Version":"2012-10-17","Statement":[...]}'
                  readOnly={!isAdmin}
                />
                {isAdmin && <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                  <button onClick={handleSavePolicy} className="btn-primary">Save Policy</button>
                  {policy.policy && (
                    <button onClick={handleDeletePolicy} className="btn-danger">Delete Policy</button>
                  )}
                </div>}
              </div>
            )}

            {activeTab === "acl" && acl && (
              <div>
                <h3>Bucket ACL</h3>
                {isAdmin && <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
                  <label style={{ fontSize: 13, fontWeight: 600 }}>Canned ACL:</label>
                  <select
                    value={bucketAclValue}
                    onChange={(e) => setBucketAclValue(e.target.value)}
                    style={{ padding: "6px 10px", border: "1px solid var(--border-input)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text)" }}
                  >
                    <option value="private">private</option>
                    <option value="public-read">public-read</option>
                    <option value="public-read-write">public-read-write</option>
                    <option value="authenticated-read">authenticated-read</option>
                  </select>
                  <button className="btn-primary btn-xs" onClick={async () => {
                    const result = await putBucketAcl(bucket, bucketAclValue);
                    if (result.supported === false) {
                      setAlertMessage(result.error || "ACL modification not supported");
                      return;
                    }
                    const newAcl = await getBucketAcl(bucket).catch(() => ({ owner: {}, grants: [] }));
                    setAcl(newAcl);
                  }}>Apply</button>
                </div>}
                <p style={{ fontSize: 13, marginBottom: 8 }}><strong>Owner:</strong> {acl.owner?.DisplayName || acl.owner?.ID || "\u2014"}</p>
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
              </div>
            )}

            {activeTab === "tags" && tags && (
              <div>
                <h3>Bucket Tags</h3>
                {Object.keys(tags.tags).length === 0 ? (
                  <p className="muted">No tags</p>
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

            {activeTab === "cors" && (
              <div>
                <h3>CORS Rules</h3>
                <textarea
                  className="code-block"
                  style={{ width: "100%", minHeight: 200, fontFamily: "monospace", resize: "vertical" }}
                  value={corsText}
                  onChange={(e) => setCorsText(e.target.value)}
                  placeholder='[{"AllowedOrigins":["*"],"AllowedMethods":["GET"],"AllowedHeaders":["*"]}]'
                  readOnly={!isAdmin}
                />
                {isAdmin && <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                  <button onClick={handleSaveCors} className="btn-primary">Save CORS</button>
                  {cors.cors_rules.length > 0 && (
                    <button onClick={handleDeleteCors} className="btn-danger">Delete CORS</button>
                  )}
                </div>}
              </div>
            )}

            {activeTab === "multipart" && (
              <div>
                {multipartLoading && <p className="muted">Loading multipart uploads...</p>}
                {multipart && <>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
                  <h3 style={{ margin: 0 }}>Incomplete Multipart Uploads ({multipart.count})</h3>
                  {isAdmin && multipart.stale_count > 0 && (
                    <button className="btn-danger btn-xs" onClick={() => setConfirmAbortAll(true)} disabled={aborting}>
                      {aborting ? "Aborting..." : `Abort ${multipart.stale_count} Stale`}
                    </button>
                  )}
                </div>
                {multipart.count > 0 && (
                  <p className="muted" style={{ margin: "0 0 8px" }}>
                    {multipart.stale_count > 0
                      ? <>Stale uploads ({">"}24h): <strong>{multipart.stale_count}</strong> using <strong>{formatSize(multipart.stale_size)}</strong></>
                      : <>All {multipart.count} uploads are recent — likely in-progress pipeline writes</>
                    }
                  </p>
                )}
                {multipart.count === 0 ? (
                  <p className="muted">No incomplete multipart uploads</p>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {multipart.uploads.map((u) => {
                      const filename = u.key.split("/").pop();
                      const folder = u.key.slice(0, u.key.length - filename.length);
                      const age = u.age_hours < 1 ? `${Math.round(u.age_hours * 60)}m ago` : u.age_hours < 48 ? `${Math.round(u.age_hours)}h ago` : `${Math.round(u.age_hours / 24)}d ago`;
                      return (
                        <div key={u.upload_id} style={{
                          padding: "10px 12px",
                          borderRadius: 8,
                          background: u.stale ? "rgba(239, 68, 68, 0.06)" : "rgba(255,255,255,0.03)",
                          border: u.stale ? "1px solid rgba(239, 68, 68, 0.2)" : "1px solid rgba(255,255,255,0.06)",
                        }}>
                          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div className="mono" style={{ fontSize: 13, fontWeight: 500, wordBreak: "break-all" }}>{filename}</div>
                              {folder && <div className="muted" style={{ fontSize: 11, marginTop: 2, wordBreak: "break-all" }}>{folder}</div>}
                            </div>
                            <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
                              {u.stale
                                ? <span style={{ color: "var(--danger, #ef4444)", fontSize: 11, fontWeight: 600, padding: "2px 6px", borderRadius: 4, background: "rgba(239, 68, 68, 0.1)" }}>Stale</span>
                                : <span style={{ color: "var(--success, #22c55e)", fontSize: 11, fontWeight: 600, padding: "2px 6px", borderRadius: 4, background: "rgba(34, 197, 94, 0.1)" }}>Active</span>
                              }
                              {isAdmin && u.stale && <button className="btn-danger btn-xs" onClick={() => handleAbortUpload(u.key, u.upload_id)}>Abort</button>}
                            </div>
                          </div>
                          <div style={{ display: "flex", gap: 16, marginTop: 6, fontSize: 12, color: "var(--text-muted, #888)" }}>
                            <span>{u.size != null ? formatSize(u.size) : "—"}</span>
                            <span>{u.part_count != null ? `${u.part_count} parts` : "—"}</span>
                            <span>{age}</span>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
                {confirmAbortAll && (
                  <ConfirmDialog
                    title="Abort Stale Multipart Uploads"
                    message={`This will abort ${multipart.stale_count} stale upload${multipart.stale_count !== 1 ? "s" : ""} (older than 24h) in "${bucket}", freeing ${formatSize(multipart.stale_size)}. ${multipart.count - multipart.stale_count} active uploads will not be touched. This cannot be undone.`}
                    confirmLabel={`Abort ${multipart.stale_count} Stale`}
                    variant="danger"
                    onConfirm={handleAbortAll}
                    onCancel={() => setConfirmAbortAll(false)}
                  />
                )}
                </>}
              </div>
            )}

            {activeTab === "index" && crawl && (
              <div>
                <h3>Object Index</h3>
                <table className="info-table">
                  <tbody>
                    <tr><td className="info-label">Status</td><td className="info-value">{crawl.status}</td></tr>
                    <tr><td className="info-label">Objects</td><td className="info-value">{(crawl.total_objects || 0).toLocaleString()}</td></tr>
                    <tr><td className="info-label">Total Size</td><td className="info-value">{formatSize(crawl.total_size || 0)}</td></tr>
                    {crawl.last_crawl_start && <tr><td className="info-label">Started</td><td className="info-value">{new Date(crawl.last_crawl_start).toLocaleString()}</td></tr>}
                    {crawl.last_crawl_end && <tr><td className="info-label">Completed</td><td className="info-value">{new Date(crawl.last_crawl_end).toLocaleString()}</td></tr>}
                  </tbody>
                </table>
                {isAdmin && <button onClick={handleRecrawl} disabled={crawl.status === "crawling"} className="btn-primary" style={{ marginTop: 12 }}>
                  {crawl.status === "crawling" ? "Indexing..." : "Re-index Bucket"}
                </button>}
              </div>
            )}
          </>
        )}
        <div className="modal-actions">
          <button onClick={onClose}>Close</button>
        </div>
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
