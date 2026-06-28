import React, { useState, useEffect, useRef, useCallback } from "react";
import BucketList from "./components/BucketList";
import Breadcrumb from "./components/Breadcrumb";
import ObjectTable from "./components/ObjectTable";
import UploadModal from "./components/UploadModal";
import DeleteDialog from "./components/DeleteDialog";
import ObjectInfo from "./components/ObjectInfo";
import BucketSettings from "./components/BucketSettings";
import SearchBar from "./components/SearchBar";
import CrawlStatus from "./components/CrawlStatus";
import Login from "./components/Login";
import ThemeToggle from "./components/ThemeToggle";
import DensityToggle from "./components/DensityToggle";
import FilePreview from "./components/FilePreview";
import Favorites from "./components/Favorites";
import AuditLog from "./components/AuditLog";
import StorageDashboard from "./components/StorageDashboard";
import FolderPicker from "./components/FolderPicker";
import PromptDialog from "./components/PromptDialog";
import Welcome from "./components/Welcome";
import SharePage from "./components/SharePage";
import TokenManager from "./components/TokenManager";
import LicenseManager from "./components/LicenseManager";
import UserManager from "./components/UserManager";
import HealthCheck from "./components/HealthCheck";
import TwoFactorSetup from "./components/TwoFactorSetup";
import EndpointManager from "./components/EndpointManager";
import ToastContainer, { toast } from "./components/Toast";
import { checkAuth, logout, refreshSession } from "./auth";
import { streamList, deleteObjects, deleteFolder, createFolder, bulkCopy, bulkMove, listDeletedVersions, purgeVersions, purgePrefix, getBranding, setCurrentEndpoint, checkForUpdate, getCrawlStatus } from "./api";

// Folder listings are fetched in keyset pages of this size so the first page
// paints instantly and no single response is huge, regardless of folder size.
const LIST_PAGE_SIZE = 1000;

// Update banner: the headline wins users get by upgrading. Refresh these to the
// latest release's user-facing highlights when cutting a new version.
const UPDATE_HIGHLIGHTS = [
  "Direct uploads, any size",
  "Million-object folders open instantly",
  "Per-user S3-key access",
  "Faster crawling",
];
const UPGRADE_CMD = "docker compose pull && docker compose up -d";

// Share link route: /share/{token}
function getShareToken() {
  const path = window.location.pathname;
  const match = path.match(/^\/share\/([a-zA-Z0-9_-]+)$/);
  return match ? match[1] : null;
}

// URL hash format:
//   #bucket:prefix          — default endpoint (backward compat)
//   #@endpoint:bucket:prefix — specific endpoint
// Empty hash = bucket list view

function parseHash() {
  const raw = decodeURIComponent(window.location.hash.slice(1));
  if (!raw) return { endpoint: "default", bucket: "", prefix: "" };
  // New format: @endpoint:bucket:prefix
  if (raw.startsWith("@")) {
    const rest = raw.substring(1);
    const idx1 = rest.indexOf(":");
    if (idx1 === -1) return { endpoint: rest, bucket: "", prefix: "" };
    const endpoint = rest.substring(0, idx1);
    const afterEndpoint = rest.substring(idx1 + 1);
    const idx2 = afterEndpoint.indexOf(":");
    if (idx2 === -1) return { endpoint, bucket: afterEndpoint, prefix: "" };
    return { endpoint, bucket: afterEndpoint.substring(0, idx2), prefix: afterEndpoint.substring(idx2 + 1) };
  }
  // Legacy format: bucket:prefix (default endpoint)
  const idx = raw.indexOf(":");
  if (idx === -1) return { endpoint: "default", bucket: raw, prefix: "" };
  return { endpoint: "default", bucket: raw.substring(0, idx), prefix: raw.substring(idx + 1) };
}

function setHash(bucket, prefix, endpoint = "default") {
  let value = "";
  if (bucket) {
    if (endpoint && endpoint !== "default") {
      value = `@${endpoint}:${bucket}${prefix ? ":" + prefix : ""}`;
    } else {
      value = prefix ? `${bucket}:${prefix}` : bucket;
    }
  }
  const newHash = value ? "#" + encodeURIComponent(value) : "";
  if (window.location.hash !== newHash) {
    if (newHash) window.location.hash = newHash;
    else history.pushState(null, "", window.location.pathname);
  }
}

export default function App() {
  // Share link route — render standalone page
  const shareToken = getShareToken();
  if (shareToken) {
    return <SharePage token={shareToken} />;
  }

  return <MainApp />;
}

function MainApp() {
  const [user, setUser] = useState(undefined);
  const [endpointId, setEndpointId] = useState(() => { const h = parseHash(); setCurrentEndpoint(h.endpoint); return h.endpoint; });
  const [bucket, setBucket] = useState(() => parseHash().bucket);
  const [prefix, setPrefix] = useState(() => parseHash().prefix);
  const [folders, setFolders] = useState([]);
  const [files, setFiles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);
  const [indexed, setIndexed] = useState(false);
  const [indexing, setIndexing] = useState(false); // current bucket's first crawl in progress (crawl-status === "crawling")
  const [indexCount, setIndexCount] = useState(0);  // objects indexed so far (drives the search-hint bar)
  const [hideSearchHint, setHideSearchHint] = useState(() => !!localStorage.getItem("sairo-search-hint-dismissed"));
  const [highlightKey, setHighlightKey] = useState(null); // file to scroll-to + highlight after "reveal in folder"
  const [selected, setSelected] = useState(new Set());
  const [selectedFolders, setSelectedFolders] = useState(new Set());
  const [showUpload, setShowUpload] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [infoKey, setInfoKey] = useState(null);
  const [filter, setFilter] = useState("");
  const [sortKey, setSortKey] = useState("name");
  const [sortAsc, setSortAsc] = useState(true);
  const [showSearch, setShowSearch] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [previewFile, setPreviewFile] = useState(null);
  const [showAuditLog, setShowAuditLog] = useState(false);
  const [dashboardBucket, setDashboardBucket] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [droppedFiles, setDroppedFiles] = useState(null);
  const [bulkAction, setBulkAction] = useState(null); // {type: "copy"|"move"}
  const [showCreateFolder, setShowCreateFolder] = useState(false);
  const [showWelcome, setShowWelcome] = useState(() => !localStorage.getItem("sairo-onboarded"));
  const [showDeleted, setShowDeleted] = useState(false);
  const [deletedItems, setDeletedItems] = useState({ folders: [], files: [] });
  const [deletedLoading, setDeletedLoading] = useState(false);
  const [showTokenManager, setShowTokenManager] = useState(false);
  const [showLicense, setShowLicense] = useState(false);
  const [showUserManager, setShowUserManager] = useState(false);
  const [showHealthCheck, setShowHealthCheck] = useState(false);
  const [showTwoFactor, setShowTwoFactor] = useState(false);
  const [showEndpointManager, setShowEndpointManager] = useState(false);
  const [branding, setBranding] = useState({ app_name: "Sairo" });
  const [updateInfo, setUpdateInfo] = useState(null);
  const [showUpgradeCmd, setShowUpgradeCmd] = useState(false);
  const [upgradeCmdCopied, setUpgradeCmdCopied] = useState(false);
  const [bucketPermission, setBucketPermission] = useState(null);
  const dragCounter = useRef(0);
  const abortRef = useRef(null);
  const refreshRef = useRef(null);
  const crawlFpRef = useRef(null);     // crawl-status fingerprint — gates the 30s silent refresh
  const silentAbortRef = useRef(null); // in-flight silent-refresh stream, so we can abort it
  const currentViewRef = useRef({ bucket: "", prefix: "" }); // guards stale refreshes from clobbering state

  const isAdmin = user && user.role === "admin";
  const canWrite = isAdmin || bucketPermission === "write";

  // Check auth on mount
  useEffect(() => {
    checkAuth().then((u) => {
      setUser(u);
      if (u) checkForUpdate().then(v => v && v.update_available && setUpdateInfo(v)).catch(() => {});
    });
    getBranding().then(setBranding).catch(() => {});
  }, []);

  // Listen for session-expired events from api.js (replaces hard page reload)
  useEffect(() => {
    const handler = () => {
      setUser(null);
      toast("Session expired. Please sign in again.", "warning", 5000);
    };
    window.addEventListener("session-expired", handler);
    return () => window.removeEventListener("session-expired", handler);
  }, []);

  // Session timeout warning — warn 5 min before expiry
  useEffect(() => {
    if (!user || !user.expires_at) return;
    const expiresMs = user.expires_at * 1000;
    const warnAt = expiresMs - 5 * 60 * 1000;
    const delay = Math.max(0, warnAt - Date.now());
    if (delay > 24 * 60 * 60 * 1000) return; // skip if > 24h away
    const timer = setTimeout(() => {
      toast("Your session expires in 5 minutes", "warning", 0, null, {
        actions: [{
          label: "Extend Session",
          onClick: async () => {
            const result = await refreshSession();
            if (result) {
              setUser(prev => ({ ...prev, expires_at: Math.floor(Date.now() / 1000) + result.expires_in }));
              toast("Session extended", "success");
            } else {
              toast("Failed to extend session", "error");
            }
          }
        }]
      });
    }, delay);
    return () => clearTimeout(timer);
  }, [user]);

  const handleLogin = useCallback(() => {
    checkAuth().then((u) => setUser(u));
  }, []);

  const handleLogout = useCallback(async () => {
    await logout();
    setUser(null);
    setBucket("");
    setPrefix("");
    setEndpointId("default");
    setCurrentEndpoint("default");
  }, []);

  const navigatePrefix = useCallback((pfx, hl) => {
    setHighlightKey(hl || null);  // optional: highlight this file once the folder loads
    setHash(bucket, pfx, endpointId);
    const current = parseHash();
    if (current.prefix === pfx && prefix === pfx) load(bucket, pfx);
  }, [bucket, prefix, endpointId]);

  const navigateBucket = useCallback((b, permission, epId) => {
    if (permission) setBucketPermission(permission);
    const ep = epId || "default";
    setEndpointId(ep);
    setCurrentEndpoint(ep);
    setHash(b, "", ep);
  }, []);

  const goHome = useCallback(() => {
    setHash("", "");
    setBucket("");
    setPrefix("");
    setEndpointId("default");
    setCurrentEndpoint("default");
    setBucketPermission(null);
  }, []);

  const load = useCallback((b, pfx) => {
    if (abortRef.current) abortRef.current.abort();
    if (silentAbortRef.current) silentAbortRef.current.abort();  // cancel any stale background refresh
    currentViewRef.current = { bucket: b, prefix: pfx };
    setFolders([]);
    setFiles([]);
    setSelected(new Set());
    setSelectedFolders(new Set());
    setFilter("");
    setLoading(true);
    setDone(false);
    setIndexed(false);
    getCrawlStatus(b).then((s) => { setIndexing(s?.status === "crawling"); setIndexCount(s?.total_objects || 0); }).catch(() => {});
    crawlFpRef.current = null;  // re-establish fingerprint for this view's background refresh

    let firstPage = true;
    abortRef.current = streamList(b, pfx, (page) => {
      if (page.folders.length > 0) setFolders((prev) => [...prev, ...page.folders]);
      if (page.files.length > 0) setFiles((prev) => [...prev, ...page.files]);
      if (page.indexed) setIndexed(true);
      if (firstPage) { setLoading(false); firstPage = false; }  // paint as soon as page 1 lands
      if (page.done) { setLoading(false); setDone(true); }
    }, (err) => {
      setLoading(false);
      setDone(true);
      if (err.status === 403 || (err.message && err.message.includes("403"))) {
        toast(`You don't have access to bucket "${b}"`, "error");
        goHome();
      } else if (err.message && err.message.includes("NoSuchBucket")) {
        toast(`Bucket "${b}" no longer exists`, "warning");
        goHome();
      } else {
        toast(`Failed to load: ${err.message}`, "error");
      }
    }, { limit: LIST_PAGE_SIZE });
  }, []);

  useEffect(() => {
    const onHashChange = () => {
      const { endpoint: ep, bucket: b, prefix: p } = parseHash();
      setEndpointId(ep);
      setCurrentEndpoint(ep);
      setBucket(b);
      setPrefix(p);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const silentRefresh = useCallback((b, pfx) => {
    // Cheap (~ms) crawl-status check first: only re-fetch the folder when the index
    // actually changed since the last load. Avoids re-downloading an unchanged folder
    // every 30s — the previous behavior re-streamed the whole listing each tick.
    getCrawlStatus(b).then((status) => {
      setIndexing(status?.status === "crawling");
      setIndexCount(status?.total_objects || 0);
      const fp = status ? `${status.total_objects}:${status.last_crawl_end}:${status.status}` : null;
      if (fp && crawlFpRef.current && fp === crawlFpRef.current) return;  // nothing changed
      crawlFpRef.current = fp;
      let newFolders = [];
      let newFiles = [];
      if (silentAbortRef.current) silentAbortRef.current.abort();  // supersede any prior in-flight refresh
      silentAbortRef.current = streamList(b, pfx, (page) => {
        if (page.folders.length > 0) newFolders = [...newFolders, ...page.folders];
        if (page.files.length > 0) newFiles = [...newFiles, ...page.files];
        if (page.indexed) setIndexed(true);
        if (page.done) {
          // Only apply if the user is still viewing this exact folder — never let a
          // late refresh of a previous folder clobber the current view.
          const v = currentViewRef.current;
          if (v.bucket === b && v.prefix === pfx) {
            setFolders(newFolders);
            setFiles(newFiles);
            setDone(true);
          }
        }
      }, null, { limit: LIST_PAGE_SIZE });
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!bucket || !user) return;
    setHash(bucket, prefix, endpointId);
    load(bucket, prefix);

    refreshRef.current = setInterval(() => {
      silentRefresh(bucket, prefix);
    }, 30000);

    return () => {
      if (abortRef.current) abortRef.current.abort();
      if (silentAbortRef.current) silentAbortRef.current.abort();
      if (refreshRef.current) clearInterval(refreshRef.current);
    };
  }, [bucket, prefix, endpointId, load, silentRefresh, user]);

  // Load deleted/versioned items when toggle is on, auto-refresh while scanning
  useEffect(() => {
    if (!showDeleted || !bucket || !user) {
      setDeletedItems({ folders: [], files: [] });
      setDeletedLoading(false);
      return;
    }
    let cancelled = false;
    let pollTimer = null;
    const fetchVersions = (isInitial) => {
      if (isInitial) setDeletedLoading(true);
      listDeletedVersions(bucket, prefix)
        .then(data => {
          if (cancelled) return;
          setDeletedItems({
            folders: data.folders || [],
            files: data.files || [],
            scan_status: data.scan_status || "none",
          });
          // Auto-poll while scan is in progress
          if (data.scan_status === "scanning") {
            pollTimer = setTimeout(() => fetchVersions(false), 5000);
          }
        })
        .catch(() => { if (!cancelled) setDeletedItems({ folders: [], files: [] }); })
        .finally(() => { if (!cancelled) setDeletedLoading(false); });
    };
    fetchVersions(true);
    return () => { cancelled = true; if (pollTimer) clearTimeout(pollTimer); };
  }, [showDeleted, bucket, prefix, user]);

  const handlePurgeDeleted = async (keys, folderPrefix) => {
    const label = folderPrefix || (keys && keys[0]) || "items";
    const toastId = toast(`Purging ${label}...`, "info", 0);
    const onProgress = (data) => {
      if (data.detail) toast(data.detail, "info", 0, toastId);
    };
    try {
      let result;
      if (folderPrefix) {
        result = await purgePrefix(bucket, folderPrefix, onProgress);
      } else if (keys && keys.length > 0) {
        result = await purgeVersions(bucket, keys, onProgress);
      }
      const purged = result?.purged || 0;
      const errors = result?.errors || 0;
      if (purged > 0) {
        toast(`Purged ${purged} version${purged !== 1 ? "s" : ""} from ${label}${errors ? ` (${errors} errors)` : ""}`, "success", 5000, toastId);
      } else {
        toast(`Cleaned up ${label} (no versioned data found)`, "info", 5000, toastId);
      }
      // Refresh deleted items and main listing
      load(bucket, prefix);
      if (showDeleted) {
        listDeletedVersions(bucket, prefix)
          .then(data => setDeletedItems({
            folders: data.folders || [], files: data.files || [],
            scan_status: data.scan_status || "none",
          }))
          .catch(() => {});
      }
    } catch (e) {
      toast(`Purge failed: ${e.message}`, "error", 5000, toastId);
    }
  };

  const handleSort = (key) => {
    if (sortKey === key) setSortAsc(!sortAsc);
    else { setSortKey(key); setSortAsc(true); }
  };

  const handleDelete = async ({ purgeVersions: doPurge = false } = {}) => {
    const fileCount = selected.size;
    const folderCount = selectedFolders.size;
    const folderList = [...selectedFolders];
    setShowDelete(false);
    let errors = 0;
    const action = doPurge ? "Purging" : "Deleting";
    const toastId = toast(
      folderCount > 0 ? `${action}... 0/${folderCount} folders` : `${action}...`,
      "info", 0,
    );
    const onProgress = (data) => {
      if (data.detail) toast(data.detail, "info", 0, toastId);
    };
    try {
      // Delete/purge files first
      if (fileCount > 0) {
        if (doPurge) {
          await purgeVersions(bucket, [...selected], onProgress);
        } else {
          await deleteObjects(bucket, [...selected]);
        }
      }
      // Delete/purge folders sequentially (large folders can take time)
      for (let i = 0; i < folderList.length; i++) {
        toast(`${action} folder ${i + 1}/${folderCount}: ${folderList[i].split("/").filter(Boolean).pop()}...`, "info", 0, toastId);
        try {
          await deleteFolder(bucket, folderList[i], doPurge, onProgress);
        } catch (e) {
          console.error("Failed to delete folder", folderList[i], e);
          errors++;
        }
      }
    } catch (e) {
      console.error("Delete failed", e);
      toast(`Delete failed: ${e.message}`, "error");
    }
    setSelected(new Set());
    setSelectedFolders(new Set());
    const parts = [];
    if (fileCount > 0) parts.push(`${fileCount} file${fileCount !== 1 ? "s" : ""}`);
    if (folderCount > 0) parts.push(`${folderCount - errors} folder${folderCount - errors !== 1 ? "s" : ""}`);
    const verb = doPurge ? "Purged" : "Deleted";
    toast(`${verb} ${parts.join(" and ")}${errors ? ` (${errors} failed)` : ""}`, errors ? "warning" : "success", 5000, toastId);
    load(bucket, prefix);
    if (showDeleted) {
      listDeletedVersions(bucket, prefix).then(setDeletedItems).catch(() => {});
    }
  };

  const handleDeleteFolders = (prefixes) => {
    // Select these folders and open delete confirmation
    setSelectedFolders(new Set(prefixes));
    setShowDelete(true);
  };

  // Bulk copy/move handler
  const handleBulkAction = async (destBucket, destPrefix) => {
    const keys = [...selected];
    const action = bulkAction.type;
    setBulkAction(null);
    const toastId = toast(`${action === "copy" ? "Copying" : "Moving"} 0/${keys.length}...`, "info", 0);
    try {
      const onProgress = ({ done, errors, total }) => {
        toast(`${action === "copy" ? "Copying" : "Moving"} ${done}/${total}${errors ? ` (${errors} failed)` : ""}...`, "info", 0, toastId);
      };
      const result = action === "copy"
        ? await bulkCopy(bucket, keys, destBucket, destPrefix, onProgress)
        : await bulkMove(bucket, keys, destBucket, destPrefix, onProgress);
      const msg = `${action === "copy" ? "Copied" : "Moved"} ${result.done} file${result.done !== 1 ? "s" : ""}${result.errors ? ` (${result.errors} failed)` : ""}`;
      toast(msg, result.errors ? "warning" : "success", 5000, toastId);
    } catch (e) {
      toast(`${action} failed: ${e.message}`, "error", 5000, toastId);
    }
    setSelected(new Set());
    setSelectedFolders(new Set());
    if (action === "move") load(bucket, prefix);
  };

  // Page-wide drag & drop
  const handleDragEnter = (e) => {
    e.preventDefault();
    dragCounter.current++;
    if (e.dataTransfer.types.includes("Files")) setDragOver(true);
  };
  const handleDragLeave = (e) => {
    e.preventDefault();
    dragCounter.current--;
    if (dragCounter.current === 0) setDragOver(false);
  };
  const handleDragOver = (e) => e.preventDefault();
  const handleDrop = (e) => {
    e.preventDefault();
    dragCounter.current = 0;
    setDragOver(false);
    if (e.dataTransfer.files.length > 0 && canWrite && bucket) {
      setDroppedFiles([...e.dataTransfer.files]);
      setShowUpload(true);
    }
  };

  // Favorites helpers (shared localStorage key with Favorites component)
  const getFavorites = useCallback(() => {
    try { return JSON.parse(localStorage.getItem("s3-browser-favorites") || "[]"); } catch { return []; }
  }, []);

  const isBreadcrumbFavorite = bucket ? getFavorites().some(f => f.bucket === bucket && f.prefix === (prefix || "") && (f.endpoint || "default") === endpointId) : false;

  const handleToggleBreadcrumbFavorite = useCallback(() => {
    if (!bucket) return;
    const favs = getFavorites();
    const idx = favs.findIndex(f => f.bucket === bucket && f.prefix === (prefix || "") && (f.endpoint || "default") === endpointId);
    if (idx >= 0) {
      favs.splice(idx, 1);
    } else {
      favs.push({ bucket, prefix: prefix || "", label: prefix ? prefix.split("/").filter(Boolean).pop() : bucket, endpoint: endpointId });
    }
    localStorage.setItem("s3-browser-favorites", JSON.stringify(favs));
  }, [bucket, prefix, endpointId, getFavorites]);

  // Navigate to a favorite
  const handleFavoriteNavigate = useCallback((favBucket, favPrefix, favEndpoint) => {
    const ep = favEndpoint || "default";
    if (favBucket !== bucket || ep !== endpointId) {
      setEndpointId(ep);
      setCurrentEndpoint(ep);
      setHash(favBucket, favPrefix, ep);
      setBucket(favBucket);
      setPrefix(favPrefix);
    } else {
      navigatePrefix(favPrefix);
    }
  }, [bucket, endpointId, navigatePrefix]);

  useEffect(() => {
    const handler = (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      // Escape closes the topmost open modal first — and must NOT also navigate the folder
      // underneath it (that competition is what made Esc feel like it needed several presses).
      if (e.key === "Escape") {
        if (dashboardBucket) { e.preventDefault(); setDashboardBucket(null); return; }
        if (previewFile)     { e.preventDefault(); setPreviewFile(null); return; }
        if (infoKey)         { e.preventDefault(); setInfoKey(null); return; }
        if (showHelp)        { e.preventDefault(); setShowHelp(false); return; }
        if (showSearch)      { e.preventDefault(); setShowSearch(false); return; }
      }
      if ((e.key === "Backspace" || e.key === "Escape") && bucket) {
        e.preventDefault();
        if (prefix) {
          const parts = prefix.split("/").filter(Boolean);
          parts.pop();
          navigatePrefix(parts.length > 0 ? parts.join("/") + "/" : "");
        } else {
          goHome();
        }
      }
      if (e.key === "/" && e.target.tagName !== "INPUT" && bucket) {
        e.preventDefault();
        setShowSearch(true);
      }
      if (e.key === "k" && (e.metaKey || e.ctrlKey) && bucket) {
        e.preventDefault();
        setShowSearch(true);
      }
      if (e.key === "?" && e.target.tagName !== "INPUT" && e.target.tagName !== "TEXTAREA") {
        e.preventDefault();
        setShowHelp((p) => !p);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [bucket, prefix, navigatePrefix, goHome, dashboardBucket, previewFile, infoKey, showHelp, showSearch]);

  // Loading auth state
  if (user === undefined) {
    return <div className="app"><div style={{ padding: "2rem", textAlign: "center" }}>Loading...</div></div>;
  }

  // Not logged in
  if (!user) {
    return (
      <div className="app">
        <Login onLogin={handleLogin} branding={branding} />
        <ToastContainer />
      </div>
    );
  }

  const appName = branding.app_name || "Sairo";

  const userBadge = (
    <div className="user-badge">
      <span className="user-name">{user.username}</span>
      <span className="user-role">{user.role}</span>
      <button onClick={() => setShowTwoFactor(true)} className="btn-settings">2FA</button>
      <button onClick={handleLogout} className="btn-settings">Logout</button>
    </div>
  );

  // Bucket list view
  if (!bucket) {
    return (
      <div className="app">
        <header>
          <div className="header-left">
            <span className="header-logo-mark">
              <svg viewBox="0 0 40 40" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" width="22" height="22">
                <path d="M28 10c0 3-4 5.5-8 5.5S12 13 12 10s4-5.5 8-5.5 8 2.5 8 5.5z"/><path d="M28 20c0 3-4 5.5-8 5.5S12 23 12 20"/><path d="M28 30c0 3-4 5.5-8 5.5S12 33 12 30"/><line x1="12" y1="10" x2="12" y2="30"/><line x1="28" y1="10" x2="28" y2="30"/>
              </svg>
            </span>
            <h1>{appName}</h1>
            <span className="bucket-name">Object Storage</span>
          </div>
          <div className="header-right">
            <Favorites onNavigate={handleFavoriteNavigate} currentBucket="" currentPrefix="" />
            {isAdmin && <button onClick={() => setShowUserManager(true)} className="btn-settings">Users</button>}
            {isAdmin && <button onClick={() => setShowTokenManager(true)} className="btn-settings">API Tokens</button>}
            {isAdmin && <button onClick={() => setShowLicense(true)} className="btn-settings">License</button>}
            {isAdmin && <button onClick={() => setShowAuditLog(true)} className="btn-settings">Activity</button>}
            {isAdmin && <button onClick={() => setShowHealthCheck(true)} className="btn-settings">Health</button>}
            {isAdmin && <button onClick={() => setShowEndpointManager(true)} className="btn-settings">Endpoints</button>}
            <ThemeToggle />
            {userBadge}
          </div>
        </header>
        {updateInfo && updateInfo.update_available && (
          <div className="update-banner">
            <span className="ub-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
            </span>
            <div className="ub-body">
              <div className="ub-head">
                Sairo <strong>v{updateInfo.latest}</strong> is here <span className="ub-cur">· you're on v{updateInfo.current}</span>
              </div>
              <div className="ub-chips">
                {UPDATE_HIGHLIGHTS.map((h) => <span className="ub-chip" key={h}>{h}</span>)}
              </div>
              <div className="ub-actions">
                <button className="ub-btn ub-primary" onClick={() => setShowUpgradeCmd((v) => !v)}>
                  Update now
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
                </button>
                <a className="ub-btn ub-link" href="https://github.com/AshwathStephen/sairo/releases/latest" target="_blank" rel="noopener noreferrer">See what's new</a>
              </div>
              {showUpgradeCmd && (
                <div className="ub-cmd">
                  <code>{UPGRADE_CMD}</code>
                  <button className="ub-copy" onClick={() => { navigator.clipboard?.writeText(UPGRADE_CMD); setUpgradeCmdCopied(true); setTimeout(() => setUpgradeCmdCopied(false), 1400); }}>{upgradeCmdCopied ? "Copied" : "Copy"}</button>
                </div>
              )}
            </div>
            <button className="ub-dismiss" aria-label="Dismiss" onClick={() => setUpdateInfo(null)}>&times;</button>
          </div>
        )}
        <BucketList onSelect={navigateBucket} role={user.role} onDashboard={setDashboardBucket} />
        {showAuditLog && <AuditLog onClose={() => setShowAuditLog(false)} />}
        {dashboardBucket && <StorageDashboard bucket={dashboardBucket} onClose={() => setDashboardBucket(null)} onNavigate={(pfx) => { setDashboardBucket(null); setHash(dashboardBucket, pfx); }} />}
        {showWelcome && <Welcome onDismiss={() => setShowWelcome(false)} />}
        {showTokenManager && <TokenManager onClose={() => setShowTokenManager(false)} />}
        {showLicense && <LicenseManager onClose={() => setShowLicense(false)} />}
        {showUserManager && <UserManager onClose={() => setShowUserManager(false)} currentUser={user} />}
        {showHealthCheck && <HealthCheck onClose={() => setShowHealthCheck(false)} />}
        {showTwoFactor && <TwoFactorSetup onClose={() => setShowTwoFactor(false)} totpEnabled={user.totp_enabled} onStatusChange={(enabled) => setUser(prev => ({ ...prev, totp_enabled: enabled }))} />}
        {showEndpointManager && <EndpointManager onClose={() => setShowEndpointManager(false)} />}
        <ToastContainer />
      </div>
    );
  }

  // Object browser view
  return (
    <div
      className="app"
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      <header>
        <div className="header-left">
          <span className="header-logo-mark" style={{ cursor: "pointer" }} onClick={goHome}>
            <svg viewBox="0 0 40 40" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" width="20" height="20">
              <path d="M28 10c0 3-4 5.5-8 5.5S12 13 12 10s4-5.5 8-5.5 8 2.5 8 5.5z"/><path d="M28 20c0 3-4 5.5-8 5.5S12 23 12 20"/><path d="M28 30c0 3-4 5.5-8 5.5S12 33 12 30"/><line x1="12" y1="10" x2="12" y2="30"/><line x1="28" y1="10" x2="28" y2="30"/>
            </svg>
          </span>
          <h1 style={{ cursor: "pointer" }} onClick={goHome}>{appName}</h1>
          <span className="bucket-name">{bucket}</span>
        </div>
        <div className="header-right">
          <CrawlStatus bucket={bucket} />
          {indexed && <span className="cache-badge">indexed</span>}
          <button onClick={() => setShowSearch(true)} className="btn-settings" title="Search (/ or ⌘K)" aria-label="Search">&#128269; Search</button>
          <button onClick={() => setDashboardBucket(bucket)} className="btn-settings" title="Storage Dashboard" aria-label="Storage Dashboard">&#128202; Insights</button>
          <button onClick={() => setShowSettings(true)} className="btn-settings" aria-label="Bucket Settings">&#9881; Settings</button>
          <Favorites onNavigate={handleFavoriteNavigate} currentBucket={bucket} currentPrefix={prefix} />
          {isAdmin && <button onClick={() => setShowAuditLog(true)} className="btn-settings">Activity</button>}
          <button onClick={() => setShowHelp(true)} className="btn-settings" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts">?</button>
          <ThemeToggle />
          {userBadge}
        </div>
      </header>

      <div className="toolbar">
        <Breadcrumb bucket={bucket} prefix={prefix} onNavigate={navigatePrefix} onHome={goHome} isFavorite={isBreadcrumbFavorite} onToggleFavorite={handleToggleBreadcrumbFavorite} />
        <div className="toolbar-actions">
          <input
            type="text"
            placeholder="Filter by name..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="filter-input"
          />
          <DensityToggle />
          <button onClick={() => load(bucket, prefix)} title="Refresh" aria-label="Refresh">&#8635;</button>
          <button
            onClick={() => setShowDeleted(!showDeleted)}
            className={showDeleted ? "btn-toggle-active" : ""}
            title="Show deleted/hidden versioned objects"
            aria-label="Show deleted objects"
            aria-pressed={showDeleted}
          >
            {showDeleted ? "Hide Deleted" : "Show Deleted"}
          </button>
          {canWrite && <button onClick={() => setShowUpload(true)}>Upload</button>}
          {canWrite && <button onClick={() => setShowCreateFolder(true)}>New Folder</button>}
          {canWrite && <button
            onClick={() => setShowDelete(true)}
            disabled={selected.size === 0 && selectedFolders.size === 0}
            className="btn-danger"
          >
            Delete ({selected.size + selectedFolders.size})
          </button>}
        </div>
      </div>

      <div className={`progress-bar ${loading ? "progress-active" : ""} ${!loading && done ? "progress-done" : ""}`}>
        <div className="progress-bar-inner" />
      </div>

      {!showDeleted && (indexing || (indexed && !hideSearchHint)) && (
        <div className={`search-hint-bar ${indexing ? "shb-indexing" : ""}`}>
          {indexing ? (
            <span className="shb-text">
              <span className="spinner shb-spinner" />
              Indexing your bucket{indexCount > 0 ? ` — ${indexCount.toLocaleString()} objects so far` : "…"} — search will be ready in a moment.
            </span>
          ) : (
            <>
              <span className="shb-text">&#128269;&nbsp; Press <kbd>/</kbd> to instantly search{indexCount > 0 ? ` across ${indexCount.toLocaleString()} objects` : " this bucket"}.</span>
              <div className="shb-actions">
                <button className="shb-search" onClick={() => setShowSearch(true)}>Search now</button>
                <button className="shb-dismiss" aria-label="Dismiss search tip" onClick={() => { setHideSearchHint(true); localStorage.setItem("sairo-search-hint-dismissed", "1"); }}>&times;</button>
              </div>
            </>
          )}
        </div>
      )}

      <ObjectTable
        bucket={bucket}
        folders={folders}
        files={files}
        filter={filter}
        selected={selected}
        selectedFolders={selectedFolders}
        onSelect={setSelected}
        onSelectFolders={setSelectedFolders}
        onNavigate={navigatePrefix}
        onFileInfo={setInfoKey}
        onFilePreview={setPreviewFile}
        onDeleteFolders={handleDeleteFolders}
        loading={loading}
        done={done}
        sortKey={sortKey}
        sortAsc={sortAsc}
        onSort={handleSort}
        indexed={indexed}
        indexing={indexing}
        onSearch={() => setShowSearch(true)}
        highlightKey={highlightKey}
        prefix={prefix}
        isAdmin={canWrite}
        showDeleted={showDeleted}
        deletedItems={deletedItems}
        deletedLoading={deletedLoading}
        onPurge={handlePurgeDeleted}
      />

      {/* Bulk action bar */}
      {(selected.size > 0 || selectedFolders.size > 0) && canWrite && (
        <div className="bulk-bar">
          <span className="bulk-bar-count">{selected.size + selectedFolders.size} selected</span>
          {selected.size > 0 && <button onClick={() => setBulkAction({ type: "copy" })}>Copy to...</button>}
          {selected.size > 0 && <button onClick={() => setBulkAction({ type: "move" })}>Move to...</button>}
          <button className="btn-danger" onClick={() => setShowDelete(true)}>Delete</button>
        </div>
      )}

      {/* Page-wide drop overlay */}
      {dragOver && canWrite && (
        <div className="drop-overlay">
          <div className="drop-overlay-text">Drop files to upload to <strong>{prefix || bucket + "/"}</strong></div>
        </div>
      )}

      {showUpload && (
        <UploadModal
          bucket={bucket}
          prefix={prefix}
          initialFiles={droppedFiles}
          onClose={() => { setShowUpload(false); setDroppedFiles(null); }}
          onUploaded={() => { setShowUpload(false); setDroppedFiles(null); toast("Upload complete", "success"); load(bucket, prefix); }}
        />
      )}
      {showDelete && (
        <DeleteDialog count={selected.size} folderCount={selectedFolders.size} fileKeys={[...selected]} folderPrefixes={[...selectedFolders]} isAdmin={canWrite} onConfirm={handleDelete} onCancel={() => { setShowDelete(false); }} />
      )}
      {infoKey && (
        <ObjectInfo bucket={bucket} fileKey={infoKey} onClose={() => setInfoKey(null)} role={user.role} />
      )}
      {previewFile && (
        <FilePreview bucket={bucket} fileKey={previewFile.key} contentType={previewFile.contentType} size={previewFile.size} onClose={() => setPreviewFile(null)} />
      )}
      {showSettings && (
        <BucketSettings bucket={bucket} onClose={() => setShowSettings(false)} role={user.role} />
      )}
      {showSearch && (
        <SearchBar bucket={bucket} prefix={prefix} onClose={() => setShowSearch(false)} onNavigate={navigatePrefix} onFileInfo={setInfoKey} onFilePreview={setPreviewFile} />
      )}
      {showAuditLog && <AuditLog onClose={() => setShowAuditLog(false)} />}
      {dashboardBucket && <StorageDashboard bucket={dashboardBucket} onClose={() => setDashboardBucket(null)} onNavigate={(pfx) => { setDashboardBucket(null); navigatePrefix(pfx); }} />}
      {bulkAction && (
        <FolderPicker
          currentBucket={bucket}
          currentPrefix={prefix}
          action={bulkAction.type}
          onSelect={handleBulkAction}
          onClose={() => setBulkAction(null)}
        />
      )}
      {showCreateFolder && (
        <PromptDialog
          title="New Folder"
          placeholder="Folder name"
          submitLabel="Create"
          onCancel={() => setShowCreateFolder(false)}
          onSubmit={async (name) => {
            setShowCreateFolder(false);
            try {
              await createFolder(bucket, prefix + name);
              toast(`Created folder "${name}"`, "success");
              load(bucket, prefix);
            } catch (e) {
              toast(`Failed to create folder: ${e.message}`, "error");
            }
          }}
        />
      )}
      {showHelp && (
        <div className="modal-overlay" onClick={() => setShowHelp(false)}>
          <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 400 }}>
            <h2>Keyboard Shortcuts</h2>
            <div className="shortcut-row"><span>Search</span><div className="shortcut-keys"><kbd className="kbd">/</kbd> or <kbd className="kbd">&#8984;K</kbd></div></div>
            <div className="shortcut-row"><span>Go back / up</span><div className="shortcut-keys"><kbd className="kbd">Backspace</kbd></div></div>
            <div className="shortcut-row"><span>Go to parent</span><div className="shortcut-keys"><kbd className="kbd">Esc</kbd></div></div>
            <div className="shortcut-row"><span>Show this help</span><div className="shortcut-keys"><kbd className="kbd">?</kbd></div></div>
            <div className="modal-actions"><button onClick={() => setShowHelp(false)}>Close</button></div>
          </div>
        </div>
      )}
      {showTwoFactor && <TwoFactorSetup onClose={() => setShowTwoFactor(false)} totpEnabled={user.totp_enabled} onStatusChange={(enabled) => setUser(prev => ({ ...prev, totp_enabled: enabled }))} />}
      <ToastContainer />
    </div>
  );
}
