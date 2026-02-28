const BASE = "/api";

// ── Multi-Endpoint Support ──────────────────────────────
let _currentEndpoint = "default";

export function setCurrentEndpoint(id) { _currentEndpoint = id || "default"; }
export function getCurrentEndpoint() { return _currentEndpoint; }

function endpointBase() {
  if (!_currentEndpoint || _currentEndpoint === "default") return BASE;
  return `${BASE}/e/${encodeURIComponent(_currentEndpoint)}`;
}

// ── Auth-aware fetch wrapper ─────────────────────────────
// On 401 (session expired), dispatch event so App can show re-login dialog
async function apiFetch(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (err) {
    if (err.name === "TypeError" && !navigator.onLine) {
      const offlineErr = new Error("You appear to be offline. Check your connection.");
      offlineErr.retryable = true;
      throw offlineErr;
    }
    throw err;
  }
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent("session-expired"));
    throw new Error("Session expired");
  }
  if (!res.ok) {
    let msg = `Error ${res.status}`;
    try {
      const body = await res.clone().json();
      if (body.detail) msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    const err = new Error(msg);
    err.status = res.status;
    err.retryable = res.status >= 500 || res.status === 429;
    throw err;
  }
  return res;
}

// ── Bucket APIs ──────────────────────────────────────────

export async function listBuckets() {
  const res = await apiFetch(`${endpointBase()}/buckets`);
  return res.json();
}

export async function createBucket(name) {
  const res = await apiFetch(`${endpointBase()}/buckets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return res.json();
}

export async function deleteBucket(name) {
  const res = await apiFetch(`${endpointBase()}/buckets/${encodeURIComponent(name)}`, { method: "DELETE" });
  return res.json();
}

// ── Object APIs (bucket-scoped) ──────────────────────────

function bucketBase(bucket) {
  return `${endpointBase()}/b/${encodeURIComponent(bucket)}`;
}

export function streamList(bucket, prefix, onPage, onError) {
  const controller = new AbortController();
  const params = new URLSearchParams({ prefix });

  apiFetch(`${bucketBase(bucket)}/list?${params}`, { signal: controller.signal })
    .then(async (res) => {
      if (!res.ok) throw new Error(`List failed: ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        for (const line of lines) {
          if (line.trim()) {
            onPage(JSON.parse(line));
          }
        }
      }
      if (buffer.trim()) {
        onPage(JSON.parse(buffer));
      }
    })
    .catch((err) => {
      if (err.name !== "AbortError") {
        console.error("Stream error:", err);
        if (onError) onError(err);
      }
    });

  return controller;
}

export async function deleteObjects(bucket, keys) {
  const res = await apiFetch(`${bucketBase(bucket)}/objects`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keys }),
  });
  if (!res.ok) throw new Error(`Delete failed: ${res.status}`);
  return res.json();
}

export async function deleteFolder(bucket, prefix, purgeVersions = false) {
  const res = await apiFetch(`${bucketBase(bucket)}/folder`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prefix, purge_versions: purgeVersions }),
  });
  return res.json();
}

export async function uploadFiles(bucket, prefix, files) {
  const form = new FormData();
  form.append("prefix", prefix);
  for (const f of files) form.append("files", f);
  const res = await apiFetch(`${bucketBase(bucket)}/upload`, { method: "POST", body: form });
  if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
  return res.json();
}

export function downloadUrl(bucket, key) {
  return `${bucketBase(bucket)}/download?key=${encodeURIComponent(key)}`;
}

export async function searchObjects(bucket, query, prefix = "", limit = 200) {
  const params = new URLSearchParams({ q: query, prefix, limit: String(limit) });
  const res = await apiFetch(`${bucketBase(bucket)}/search?${params}`);
  if (!res.ok) throw new Error(`Search failed: ${res.status}`);
  return res.json();
}

export async function getCrawlStatus(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/crawl-status`);
  if (!res.ok) throw new Error(`Crawl status failed: ${res.status}`);
  return res.json();
}

export async function triggerCrawl(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/crawl`, { method: "POST" });
  if (!res.ok) throw new Error(`Trigger crawl failed: ${res.status}`);
  return res.json();
}

export async function getFolderSize(bucket, prefix) {
  const params = new URLSearchParams({ prefix });
  const res = await apiFetch(`${bucketBase(bucket)}/folder-size?${params}`);
  if (!res.ok) throw new Error(`Folder size failed: ${res.status}`);
  return res.json();
}

export async function getStorageBreakdown(bucket, prefix = "") {
  const params = new URLSearchParams({ prefix });
  const res = await apiFetch(`${bucketBase(bucket)}/storage-breakdown?${params}`);
  if (!res.ok) throw new Error(`Storage breakdown failed: ${res.status}`);
  return res.json();
}

export async function getStorageHistory(bucket, prefix = "", days = 90) {
  const params = new URLSearchParams({ prefix, days: String(days) });
  const res = await apiFetch(`${bucketBase(bucket)}/storage-history?${params}`);
  if (!res.ok) throw new Error(`Storage history failed: ${res.status}`);
  return res.json();
}

// ── Bucket Config APIs ──────────────────────────────────

export async function getVersioning(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/versioning`);
  return res.json();
}

export async function putVersioning(bucket, enabled) {
  const res = await apiFetch(`${bucketBase(bucket)}/versioning?enabled=${enabled}`, { method: "PUT" });
  if (!res.ok) throw new Error(`Set versioning failed: ${res.status}`);
  return res.json();
}

export async function getLifecycle(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/lifecycle`);
  return res.json();
}

export async function putLifecycle(bucket, rules) {
  const res = await apiFetch(`${bucketBase(bucket)}/lifecycle`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rules }),
  });
  if (!res.ok) throw new Error(`Set lifecycle failed: ${res.status}`);
  return res.json();
}

export async function deleteLifecycle(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/lifecycle`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Delete lifecycle failed: ${res.status}`);
  return res.json();
}

export async function getCors(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/cors`);
  return res.json();
}

export async function putCors(bucket, corsRules) {
  const res = await apiFetch(`${bucketBase(bucket)}/cors`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cors_rules: corsRules }),
  });
  if (!res.ok) throw new Error(`Set CORS failed: ${res.status}`);
  return res.json();
}

export async function deleteCors(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/cors`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Delete CORS failed: ${res.status}`);
  return res.json();
}

export async function getBucketPolicy(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/policy`);
  return res.json();
}

export async function putBucketPolicy(bucket, policy) {
  const res = await apiFetch(`${bucketBase(bucket)}/policy`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ policy }),
  });
  return res.json();
}

export async function deleteBucketPolicy(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/policy`, { method: "DELETE" });
  return res.json();
}

export async function getBucketAcl(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/acl`);
  return res.json();
}

export async function putBucketAcl(bucket, acl) {
  const res = await apiFetch(`${bucketBase(bucket)}/acl`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ acl }),
  });
  if (!res.ok) throw new Error(`Set bucket ACL failed: ${res.status}`);
  return res.json();
}

export async function getBucketTagging(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/tagging`);
  return res.json();
}

export async function putBucketTagging(bucket, tags) {
  const res = await apiFetch(`${bucketBase(bucket)}/tagging`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tags }),
  });
  return res.json();
}

export async function getObjectTagging(bucket, key) {
  const res = await apiFetch(`${bucketBase(bucket)}/object-tagging?key=${encodeURIComponent(key)}`);
  return res.json();
}

export async function putObjectTagging(bucket, key, tags) {
  const res = await apiFetch(`${bucketBase(bucket)}/object-tagging?key=${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tags }),
  });
  return res.json();
}

export async function getObjectAcl(bucket, key) {
  const res = await apiFetch(`${bucketBase(bucket)}/object-acl?key=${encodeURIComponent(key)}`);
  return res.json();
}

export async function putObjectAcl(bucket, key, acl) {
  const res = await apiFetch(`${bucketBase(bucket)}/object-acl?key=${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ acl }),
  });
  if (!res.ok) throw new Error(`Set object ACL failed: ${res.status}`);
  return res.json();
}

export async function getMultipartUploads(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/multipart-uploads`);
  return res.json();
}

export async function abortMultipart(bucket, key, uploadId) {
  const res = await apiFetch(`${bucketBase(bucket)}/abort-multipart`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, upload_id: uploadId }),
  });
  return res.json();
}

export async function getObjectInfo(bucket, key) {
  const res = await apiFetch(`${bucketBase(bucket)}/object-info?key=${encodeURIComponent(key)}`);
  return res.json();
}

export async function getObjectVersions(bucket, key) {
  const res = await apiFetch(`${bucketBase(bucket)}/object-versions?key=${encodeURIComponent(key)}`);
  return res.json();
}

export async function getPresignedUrl(bucket, key, expires = 3600) {
  const res = await apiFetch(`${bucketBase(bucket)}/presigned-url?key=${encodeURIComponent(key)}&expires=${expires}`);
  return res.json();
}

export async function getObjectLock(bucket) {
  const res = await apiFetch(`${bucketBase(bucket)}/object-lock`);
  return res.json();
}

export async function refreshPrefix(bucket, prefix = "") {
  const params = new URLSearchParams({ prefix });
  const res = await apiFetch(`${bucketBase(bucket)}/refresh-prefix?${params}`, { method: "POST" });
  if (!res.ok) throw new Error(`Refresh failed: ${res.status}`);
  return res.json();
}

export async function createFolder(bucket, prefix) {
  const res = await apiFetch(`${bucketBase(bucket)}/create-folder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prefix }),
  });
  if (!res.ok) throw new Error(`Create folder failed: ${res.status}`);
  return res.json();
}

export async function copyObject(bucket, sourceKey, destKey, destBucket = null) {
  const res = await apiFetch(`${bucketBase(bucket)}/copy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_key: sourceKey, dest_key: destKey, dest_bucket: destBucket }),
  });
  if (!res.ok) throw new Error(`Copy failed: ${res.status}`);
  return res.json();
}

export async function renameObject(bucket, sourceKey, destKey) {
  const res = await apiFetch(`${bucketBase(bucket)}/rename`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_key: sourceKey, dest_key: destKey }),
  });
  if (!res.ok) throw new Error(`Rename failed: ${res.status}`);
  return res.json();
}

// ── Bulk Operations ─────────────────────────────────────

export async function bulkCopy(bucket, keys, destBucket, destPrefix, onProgress) {
  let done = 0, errors = 0;
  for (const key of keys) {
    const filename = key.split("/").pop();
    const destKey = destPrefix + filename;
    try {
      await copyObject(bucket, key, destKey, destBucket);
      done++;
    } catch {
      errors++;
    }
    if (onProgress) onProgress({ done, errors, total: keys.length });
  }
  return { done, errors, total: keys.length };
}

export async function bulkMove(bucket, keys, destBucket, destPrefix, onProgress) {
  let done = 0, errors = 0;
  for (const key of keys) {
    const filename = key.split("/").pop();
    const destKey = destPrefix + filename;
    try {
      if (destBucket && destBucket !== bucket) {
        await copyObject(bucket, key, destKey, destBucket);
        await deleteObjects(bucket, [key]);
      } else {
        await renameObject(bucket, key, destKey);
      }
      done++;
    } catch {
      errors++;
    }
    if (onProgress) onProgress({ done, errors, total: keys.length });
  }
  return { done, errors, total: keys.length };
}

// ── File Preview ────────────────────────────────────────

export async function fetchPreview(bucket, key, maxBytes) {
  const params = new URLSearchParams({ key });
  if (maxBytes != null) params.set("max_bytes", String(maxBytes));
  const res = await apiFetch(`${bucketBase(bucket)}/preview?${params}`);
  if (!res.ok) throw new Error(`Preview failed: ${res.status}`);
  return res.json();
}

export async function fetchPreviewTail(bucket, key, maxBytes = 512000) {
  const params = new URLSearchParams({ key, max_bytes: String(maxBytes) });
  const res = await apiFetch(`${bucketBase(bucket)}/preview-tail?${params}`);
  if (!res.ok) throw new Error(`Tail preview failed: ${res.status}`);
  return res.json();
}

export async function fetchFileMetadata(bucket, key) {
  const params = new URLSearchParams({ key });
  const res = await apiFetch(`${bucketBase(bucket)}/file-metadata?${params}`);
  if (!res.ok) throw new Error(`Metadata failed: ${res.status}`);
  return res.json();
}

// ── Version Operations ──────────────────────────────────

export async function versionRestore(bucket, key, versionId) {
  const res = await apiFetch(`${bucketBase(bucket)}/version-restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, version_id: versionId }),
  });
  if (!res.ok) throw new Error(`Restore failed: ${res.status}`);
  return res.json();
}

export async function versionDelete(bucket, key, versionId) {
  const res = await apiFetch(`${bucketBase(bucket)}/version-delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, version_id: versionId }),
  });
  if (!res.ok) throw new Error(`Delete version failed: ${res.status}`);
  return res.json();
}

export async function getVersionPresignedUrl(bucket, key, versionId, expires = 3600) {
  const params = new URLSearchParams({ key, version_id: versionId, expires: String(expires) });
  const res = await apiFetch(`${bucketBase(bucket)}/version-presigned-url?${params}`);
  if (!res.ok) throw new Error(`Version URL failed: ${res.status}`);
  return res.json();
}

// ── Upload with Progress ────────────────────────────────

export function getUploadUrl(bucket) {
  return `${bucketBase(bucket)}/upload`;
}

export function uploadFileWithProgress(bucket, prefix, file, onProgress) {
  const xhr = new XMLHttpRequest();
  const form = new FormData();
  form.append("prefix", prefix);
  form.append("files", file);

  const promise = new Promise((resolve, reject) => {
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress({ loaded: e.loaded, total: e.total, percent: (e.loaded / e.total) * 100 });
      }
    });
    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
      else reject(new Error(`Upload failed: ${xhr.status}`));
    });
    xhr.addEventListener("error", () => reject(new Error("Upload network error")));
    xhr.addEventListener("abort", () => {
      const err = new Error("Upload aborted");
      err.name = "AbortError";
      reject(err);
    });
  });

  xhr.open("POST", `${bucketBase(bucket)}/upload`);
  xhr.send(form);

  return { xhr, promise, abort: () => xhr.abort() };
}

// ── Version Browsing & Purging ────────────────────────────

export async function listDeletedVersions(bucket, prefix = "") {
  const params = new URLSearchParams({ prefix, show: "deleted" });
  const res = await apiFetch(`${bucketBase(bucket)}/list-versions?${params}`);
  if (!res.ok) throw new Error(`List versions failed: ${res.status}`);
  return res.json();
}

export async function purgeVersions(bucket, keys) {
  const res = await apiFetch(`${bucketBase(bucket)}/purge-versions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keys }),
  });
  if (!res.ok) throw new Error(`Purge failed: ${res.status}`);
  return res.json();
}

export async function purgePrefix(bucket, prefix) {
  const res = await apiFetch(`${bucketBase(bucket)}/purge-versions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prefix }),
  });
  if (!res.ok) throw new Error(`Purge prefix failed: ${res.status}`);
  return res.json();
}

// ── Audit Log ───────────────────────────────────────────

export async function getAuditLog({ limit = 50, offset = 0, action = "", username = "", bucket = "" } = {}) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (action) params.set("action", action);
  if (username) params.set("username", username);
  if (bucket) params.set("bucket", bucket);
  const res = await apiFetch(`${BASE}/audit-log?${params}`);
  if (!res.ok) throw new Error(`Audit log failed: ${res.status}`);
  return res.json();
}

// ── Helpers ──────────────────────────────────────────────

// ── User Management ────────────────────────────────────

export async function listUsers() {
  const res = await apiFetch(`${BASE}/auth/users`);
  return res.json();
}

export async function createUser(username, password, role) {
  const res = await apiFetch(`${BASE}/auth/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, role }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to create user");
  }
  return res.json();
}

export async function updateUserRole(username, role) {
  const res = await apiFetch(`${BASE}/auth/users/${encodeURIComponent(username)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to update user");
  }
  return res.json();
}

export async function deleteUser(username) {
  const res = await apiFetch(`${BASE}/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to delete user");
  }
  return res.json();
}

// ── Bucket Permissions ──────────────────────────────────

export async function getUserPermissions(username) {
  const res = await apiFetch(`${BASE}/auth/users/${encodeURIComponent(username)}/permissions`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to get permissions");
  }
  return res.json();
}

export async function setUserPermissions(username, permissions) {
  const res = await apiFetch(`${BASE}/auth/users/${encodeURIComponent(username)}/permissions`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ permissions }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to set permissions");
  }
  return res.json();
}

export async function removeUserPermission(username, bucket) {
  const res = await apiFetch(`${BASE}/auth/users/${encodeURIComponent(username)}/permissions/${encodeURIComponent(bucket)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to remove permission");
  }
  return res.json();
}

// ── API Tokens ──────────────────────────────────────────

export async function listTokens() {
  const res = await apiFetch(`${BASE}/auth/tokens`);
  return res.json();
}

export async function createToken(name, role, expiresDays) {
  const res = await apiFetch(`${BASE}/auth/tokens`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, role, expires_days: expiresDays || null }),
  });
  return res.json();
}

export async function deleteToken(tokenId) {
  const res = await apiFetch(`${BASE}/auth/tokens/${tokenId}`, { method: "DELETE" });
  return res.json();
}

// ── Share Links ─────────────────────────────────────────

export async function createShareLink(bucket, key, expiresHours = 168, maxDownloads = null, password = null) {
  const res = await apiFetch(`${BASE}/share-links`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bucket, key, expires_hours: expiresHours, max_downloads: maxDownloads, password }),
  });
  return res.json();
}

export async function listShareLinks(bucket = "") {
  const url = bucket ? `${BASE}/share-links?bucket=${encodeURIComponent(bucket)}` : `${BASE}/share-links`;
  const res = await apiFetch(url);
  return res.json();
}

export async function deleteShareLink(linkId) {
  const res = await apiFetch(`${BASE}/share-links/${linkId}`, { method: "DELETE" });
  return res.json();
}

export async function resolveShareLink(token, password = "") {
  const url = password
    ? `${BASE}/share/${token}?password=${encodeURIComponent(password)}`
    : `${BASE}/share/${token}`;
  const res = await fetch(url);  // No apiFetch — this is a public endpoint
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Error ${res.status}`);
  }
  return res.json();
}

// ── License ─────────────────────────────────────────────

export async function getLicense() {
  const res = await apiFetch(`${BASE}/license`);
  return res.json();
}

export async function activateLicense(key) {
  const res = await apiFetch(`${BASE}/license`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key }),
  });
  return res.json();
}

// ── Branding ────────────────────────────────────────────

export async function getBranding() {
  const res = await fetch(`${BASE}/branding`);  // No apiFetch — public endpoint
  if (!res.ok) return { app_name: "Sairo", primary_color: "#3b82f6" };
  return res.json();
}

// ── S3 Health Check ─────────────────────────────────────

export async function getS3Health(endpointId = "") {
  const params = endpointId ? `?endpoint_id=${encodeURIComponent(endpointId)}` : "";
  const res = await apiFetch(`${BASE}/health/s3${params}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Health check failed");
  }
  return res.json();
}

export async function refreshS3Health(endpointId = "") {
  const params = endpointId ? `?endpoint_id=${encodeURIComponent(endpointId)}` : "";
  const res = await apiFetch(`${BASE}/health/s3/refresh${params}`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Health check refresh failed");
  }
  return res.json();
}

// ── System Info ──────────────────────────────────────────

export async function getSystemInfo() {
  const res = await apiFetch(`${BASE}/system-info`);
  return res.json();
}

export async function getHealthDetail() {
  const res = await apiFetch(`${BASE}/health-detail`);
  return res.json();
}

// ── LDAP ────────────────────────────────────────────────

export async function ldapLogin(username, password) {
  const res = await fetch(`${BASE}/auth/ldap`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "LDAP login failed");
  }
  return res.json();
}

// ── 2FA / TOTP ────────────────────────────────────────────

export async function setup2FA() {
  const res = await apiFetch(`${BASE}/auth/2fa/setup`, { method: "POST" });
  if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "2FA setup failed"); }
  return res.json();
}

export async function enable2FA(code) {
  const res = await apiFetch(`${BASE}/auth/2fa/enable`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
  });
  if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "2FA enable failed"); }
  return res.json();
}

export async function disable2FA(password) {
  const res = await apiFetch(`${BASE}/auth/2fa/disable`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }),
  });
  if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "2FA disable failed"); }
  return res.json();
}

export async function reset2FA(username) {
  const res = await apiFetch(`${BASE}/auth/2fa/reset/${encodeURIComponent(username)}`, { method: "POST" });
  if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "2FA reset failed"); }
  return res.json();
}

export async function verify2FA(code) {
  const res = await fetch(`${BASE}/auth/2fa/verify`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
  });
  if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "Invalid code"); }
  return res.json();
}

export async function recover2FA(code) {
  const res = await fetch(`${BASE}/auth/2fa/recover`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
  });
  if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "Invalid recovery code"); }
  return res.json();
}

// ── Endpoint Management ─────────────────────────────────

export async function listEndpoints() {
  const res = await apiFetch(`${BASE}/endpoints`);
  return res.json();
}

export async function listAllBuckets() {
  const res = await apiFetch(`${BASE}/all-buckets`);
  return res.json();
}

export async function createEndpoint(endpoint) {
  const res = await apiFetch(`${BASE}/endpoints`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(endpoint),
  });
  if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "Failed to create endpoint"); }
  return res.json();
}

export async function updateEndpoint(id, endpoint) {
  const res = await apiFetch(`${BASE}/endpoints/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(endpoint),
  });
  if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "Failed to update endpoint"); }
  return res.json();
}

export async function deleteEndpoint(id) {
  const res = await apiFetch(`${BASE}/endpoints/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "Failed to delete endpoint"); }
  return res.json();
}

export async function testEndpoint(id) {
  const res = await apiFetch(`${BASE}/endpoints/${encodeURIComponent(id)}/test`, { method: "POST" });
  if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "Connection test failed"); }
  return res.json();
}

// ── Helpers ─────────────────────────────────────────────

export function formatSize(bytes) {
  if (bytes == null || bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return (bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0) + " " + units[i];
}

export function formatDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}
