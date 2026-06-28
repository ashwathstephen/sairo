import React, { useMemo, useRef, useState, useEffect } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { formatSize, formatDate, downloadUrl, getStorageBreakdown } from "../api";
import ConfirmDialog from "./ConfirmDialog";
import {
  Folder, File, FileImage, FileVideo, FileAudio, FileCode2,
  FileText, FileArchive, Sheet, FileJson, Database, FileType,
} from "lucide-react";

const PREVIEW_EXTS = new Set(["jpg","jpeg","png","gif","svg","webp","ico","bmp","txt","log","out","err","md","json","csv","xml","yaml","yml","js","jsx","ts","tsx","py","sql","sh","bash","conf","cfg","ini","html","css","toml","pdf","parquet","orc","avro"]);

export function canPreview(name) {
  const dot = name.lastIndexOf(".");
  return dot >= 0 && PREVIEW_EXTS.has(name.substring(dot + 1).toLowerCase());
}

const ICON_MAP = {
  image: FileImage, video: FileVideo, audio: FileAudio,
  code: FileCode2, text: FileText, archive: FileArchive,
  spreadsheet: Sheet, data: Database, pdf: FileType, json: FileJson,
};

const EXT_CATEGORIES = {
  jpg: "image", jpeg: "image", png: "image", gif: "image", svg: "image",
  webp: "image", ico: "image", bmp: "image", tiff: "image",
  mp4: "video", mov: "video", avi: "video", mkv: "video", webm: "video",
  mp3: "audio", wav: "audio", flac: "audio", ogg: "audio", aac: "audio",
  js: "code", jsx: "code", ts: "code", tsx: "code", py: "code",
  go: "code", rs: "code", java: "code", rb: "code", php: "code",
  sh: "code", bash: "code", sql: "code", html: "code", css: "code",
  yaml: "code", yml: "code", toml: "code", xml: "code",
  conf: "code", cfg: "code", ini: "code",
  txt: "text", md: "text", log: "text", readme: "text", out: "text", err: "text",
  csv: "spreadsheet", tsv: "spreadsheet", xls: "spreadsheet", xlsx: "spreadsheet",
  zip: "archive", tar: "archive", gz: "archive", bz2: "archive",
  rar: "archive", "7z": "archive", zst: "archive",
  parquet: "data", avro: "data", orc: "data",
  json: "json",
  pdf: "pdf",
};

function getFileIcon(name) {
  const dot = name.lastIndexOf(".");
  if (dot < 0) return File;
  const ext = name.substring(dot + 1).toLowerCase();
  return ICON_MAP[EXT_CATEGORIES[ext]] || File;
}

export default function ObjectTable({
  bucket,
  folders,
  files,
  filter,
  selected,
  selectedFolders,
  onSelect,
  onSelectFolders,
  onNavigate,
  onFileInfo,
  onFilePreview,
  onDeleteFolders,
  loading,
  done,
  sortKey,
  sortAsc,
  onSort,
  indexed,
  indexing,
  onSearch,
  highlightKey,
  prefix,
  isAdmin,
  showDeleted,
  deletedItems,
  deletedLoading,
  onPurge,
}) {
  const [folderSizes, setFolderSizes] = useState({});
  const [confirmPurge, setConfirmPurge] = useState(null); // {keys, prefix, label}
  const [compact, setCompact] = useState(() => document.documentElement.dataset.density === "compact");
  const parentRef = useRef(null);

  useEffect(() => {
    const handler = () => setCompact(document.documentElement.dataset.density === "compact");
    window.addEventListener("density-change", handler);
    return () => window.removeEventListener("density-change", handler);
  }, []);

  useEffect(() => {
    if (!indexed || folders.length === 0) {
      setFolderSizes({});
      return;
    }
    getStorageBreakdown(bucket, prefix)
      .then((data) => {
        const sizes = {};
        for (const child of data.children) {
          sizes[child.prefix] = { size: child.total_size, count: child.object_count };
        }
        setFolderSizes(sizes);
      })
      .catch(() => {});
  }, [indexed, folders, prefix, bucket]);

  const lowerFilter = filter.toLowerCase();
  const filteredFolders = useMemo(() => {
    let filtered = filter
      ? folders.filter((f) => f.name.toLowerCase().includes(lowerFilter))
      : [...folders];

    if (sortKey === "size" || sortKey === "last_modified") {
      filtered.sort((a, b) => {
        const aInfo = folderSizes[a.prefix];
        const bInfo = folderSizes[b.prefix];
        let cmp = 0;
        if (sortKey === "size") {
          cmp = (aInfo?.size || 0) - (bInfo?.size || 0);
        } else {
          cmp = (aInfo?.count || 0) - (bInfo?.count || 0);
        }
        return sortAsc ? cmp : -cmp;
      });
    } else if (sortKey === "name") {
      filtered.sort((a, b) => {
        const cmp = a.name.localeCompare(b.name);
        return sortAsc ? cmp : -cmp;
      });
    }
    return filtered;
  }, [folders, filter, lowerFilter, sortKey, sortAsc, folderSizes]);

  const sortedFiles = useMemo(() => {
    let filtered = filter
      ? files.filter((f) => f.name.toLowerCase().includes(lowerFilter))
      : [...files];

    filtered.sort((a, b) => {
      let cmp = 0;
      if (sortKey === "name") cmp = a.name.localeCompare(b.name);
      else if (sortKey === "size") cmp = a.size - b.size;
      else if (sortKey === "last_modified") cmp = a.last_modified.localeCompare(b.last_modified);
      return sortAsc ? cmp : -cmp;
    });
    return filtered;
  }, [files, filter, lowerFilter, sortKey, sortAsc]);

  const hasFolders = filteredFolders.length > 0;
  const hasFiles = sortedFiles.length > 0;

  const rows = useMemo(() => {
    const folderRows = filteredFolders.map((f) => ({ type: "folder", data: f }));
    const fileRows = sortedFiles.map((f) => ({ type: "file", data: f }));
    return [...folderRows, ...fileRows];
  }, [filteredFolders, sortedFiles]);

  const rowHeight = compact ? 28 : 36;
  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => rowHeight,
    overscan: 20,
  });

  // "Reveal in folder" from search: scroll to + briefly highlight the target file.
  // Consume each highlightKey once so a background refresh doesn't re-scroll the user.
  const [highlightedKey, setHighlightedKey] = useState(null);
  const consumedHlRef = useRef(null);
  useEffect(() => {
    if (!highlightKey) { consumedHlRef.current = null; return; }
    if (highlightKey === consumedHlRef.current) return;
    const idx = rows.findIndex((row) => row.type === "file" && row.data.key === highlightKey);
    if (idx < 0) return; // target not in this folder's rows yet — wait for them to load
    consumedHlRef.current = highlightKey;
    rowVirtualizer.scrollToIndex(idx, { align: "center" });
    setHighlightedKey(highlightKey);
    const t = setTimeout(() => setHighlightedKey(null), 3000);
    return () => clearTimeout(t);
  }, [highlightKey, rows]);

  const allFileKeys = sortedFiles.map((f) => f.key);
  const allFolderPrefixes = filteredFolders.map((f) => f.prefix);
  const allSelected =
    (allFileKeys.length > 0 || allFolderPrefixes.length > 0) &&
    allFileKeys.every((k) => selected.has(k)) &&
    allFolderPrefixes.every((p) => selectedFolders.has(p));

  const toggleAll = () => {
    if (allSelected) {
      onSelect(new Set());
      onSelectFolders(new Set());
    } else {
      onSelect(new Set(allFileKeys));
      onSelectFolders(new Set(allFolderPrefixes));
    }
  };

  const toggleOne = (key) => {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onSelect(next);
  };

  const toggleFolder = (prefix) => {
    const next = new Set(selectedFolders);
    if (next.has(prefix)) next.delete(prefix);
    else next.add(prefix);
    onSelectFolders(next);
  };

  const sortIcon = (key) => {
    if (sortKey !== key) return " \u2195";
    return sortAsc ? " \u2191" : " \u2193";
  };

  const isEmpty = rows.length === 0 && !loading && done;
  const isLoading = rows.length === 0 && loading;

  return (
    <div className="table-container">
      <div className="table-header-row">
        <div className="th col-check">
          <input type="checkbox" checked={allSelected} onChange={toggleAll} />
        </div>
        <div className="th col-name sortable" onClick={() => onSort("name")}>
          Name{sortIcon("name")}
        </div>
        <div className="th col-size sortable" onClick={() => onSort("size")}>
          Size{sortIcon("size")}
        </div>
        <div className="th col-modified sortable" onClick={() => onSort("last_modified")}>
          {hasFolders && !hasFiles ? "Objects" : "Last Modified"}{sortIcon("last_modified")}
        </div>
        <div className="th col-actions">Actions</div>
      </div>

      <div ref={parentRef} className="table-scroll-area">
        {isEmpty && indexing && !filter && (
          <div className="empty-state">
            <div className="empty-state-icon"><div className="spinner" /></div>
            <h3 className="empty-state-title">Indexing your bucket\u2026</h3>
            <p className="empty-state-text">
              Building a fast index of your objects \u2014 browsing and instant search will be ready in a moment.
            </p>
            {onSearch && (
              <button className="btn-primary" style={{ marginTop: 12 }} onClick={onSearch}>&#128269; Try a search</button>
            )}
          </div>
        )}
        {isEmpty && !(indexing && !filter) && (
          <div className="empty-state">
            <div className="empty-state-icon">{filter ? "\uD83D\uDD0D" : "\uD83D\uDCC2"}</div>
            <h3 className="empty-state-title">{filter ? "No matching items" : "This folder is empty"}</h3>
            <p className="empty-state-text">
              {filter ? `No files or folders match "${filter}"` : isAdmin ? "Upload files or create a folder to get started" : "There are no files in this location"}
            </p>
          </div>
        )}
        {isLoading && (
          <div className="empty"><div className="spinner" /> Loading...</div>
        )}
        {rows.length > 0 && (
          <div style={{ height: `${rowVirtualizer.getTotalSize()}px`, width: "100%", position: "relative" }}>
            {rowVirtualizer.getVirtualItems().map((virtualRow) => {
              const row = rows[virtualRow.index];
              if (row.type === "folder") {
                const folder = row.data;
                const sizeInfo = folderSizes[folder.prefix];
                return (
                  <div
                    key={folder.prefix}
                    className={`table-row row-folder ${selectedFolders.has(folder.prefix) ? "row-selected" : ""}`}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      height: `${virtualRow.size}px`,
                      transform: `translateY(${virtualRow.start}px)`,
                    }}
                  >
                    <div className="td col-check col-check-cell">
                      <input
                        type="checkbox"
                        checked={selectedFolders.has(folder.prefix)}
                        onChange={() => toggleFolder(folder.prefix)}
                      />
                    </div>
                    <div className="td col-name">
                      <a
                        href="#"
                        onClick={(e) => { e.preventDefault(); onNavigate(folder.prefix); }}
                        className="folder-link"
                      >
                        <Folder size={16} className="file-icon folder-icon" /> {folder.name}/
                      </a>
                    </div>
                    <div className="td col-size muted">
                      {sizeInfo ? formatSize(sizeInfo.size) : "—"}
                    </div>
                    <div className="td col-modified muted">
                      {sizeInfo ? `${sizeInfo.count.toLocaleString()} objects` : "—"}
                    </div>
                    <div className="td col-actions"></div>
                  </div>
                );
              } else {
                const file = row.data;
                return (
                  <div
                    key={file.key}
                    className={`table-row ${selected.has(file.key) ? "row-selected" : ""} ${file.key === highlightedKey ? "row-highlight" : ""}`}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      height: `${virtualRow.size}px`,
                      transform: `translateY(${virtualRow.start}px)`,
                    }}
                  >
                    <div className="td col-check col-check-cell">
                      <input
                        type="checkbox"
                        checked={selected.has(file.key)}
                        onChange={() => toggleOne(file.key)}
                      />
                    </div>
                    <div className="td col-name" title={file.key}>
                      {React.createElement(getFileIcon(file.name), { size: 16, className: "file-icon" })}
                      {file.name}
                    </div>
                    <div className="td col-size">{formatSize(file.size)}</div>
                    <div className="td col-modified">{formatDate(file.last_modified)}</div>
                    <div className="td col-actions actions-cell">
                      {canPreview(file.name) && onFilePreview && (
                        <><button className="btn-small" onClick={() => onFilePreview({ key: file.key, size: file.size })} title="Preview" aria-label="Preview file">&#128065;</button>{" "}</>
                      )}
                      <a href={downloadUrl(bucket, file.key)} className="btn-small" title="Download" aria-label="Download file">&#8595;</a>
                      {" "}
                      <button className="btn-small btn-info" onClick={() => onFileInfo(file.key)} title="Info" aria-label="File info">i</button>
                    </div>
                  </div>
                );
              }
            })}
          </div>
        )}
      </div>

      <div className="table-footer">
        {loading && (folders.length > 0 || files.length > 0) && (
          <div className="loading streaming-indicator">
            <div className="streaming-dot" />
            <span>Streaming</span>
            <span className="streaming-count">{folders.length} folders, {files.length} files</span>
          </div>
        )}
        {!loading && (
          <span className="count">
            {filteredFolders.length} folder{filteredFolders.length !== 1 ? "s" : ""}, {sortedFiles.length} file{sortedFiles.length !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {/* Deleted / Hidden Versioned Items */}
      {showDeleted && deletedLoading && (
        <div className="deleted-section">
          <div className="deleted-section-header">
            <span>Scanning for versioned objects...</span>
            <div className="spinner" />
          </div>
        </div>
      )}
      {showDeleted && !deletedLoading && deletedItems && deletedItems.scan_status === "scanning" && deletedItems.folders.length === 0 && (
        <div className="deleted-section">
          <div className="deleted-section-header">
            <span>Version scan in progress (first scan takes a few minutes)...</span>
            <div className="spinner" />
          </div>
        </div>
      )}
      {showDeleted && !deletedLoading && deletedItems && deletedItems.folders.length > 0 && (
        <div className="deleted-section">
          <div className="deleted-section-header">
            <span>Deleted / Hidden Objects</span>
            <span className="deleted-section-count">
              {deletedItems.folders.length} folder{deletedItems.folders.length !== 1 ? "s" : ""} with version history
              {deletedItems.scan_status === "scanning" && " (scan updating...)"}
            </span>
          </div>
          {deletedItems.folders.map(f => (
            <div key={f.prefix} className="table-row row-deleted">
              <div className="td col-check"></div>
              <div className="td col-name deleted-name">
                <span className={f.has_current_objects ? "version-badge-active" : "version-badge-deleted"}>
                  {f.has_current_objects ? "Active" : "Deleted"}
                </span>
                {" "}&#128193; {f.prefix.replace(/\/$/, "").split("/").pop()}/
                <span className="deleted-meta">
                  {f.versions_count > 0 && <>{f.versions_count} version{f.versions_count !== 1 ? "s" : ""}</>}
                  {f.versions_count > 0 && f.delete_markers_count > 0 && ", "}
                  {f.delete_markers_count > 0 && <>{f.delete_markers_count} delete marker{f.delete_markers_count !== 1 ? "s" : ""}</>}
                  {f.versions_count === 0 && f.delete_markers_count === 0 && "empty ghost"}
                </span>
              </div>
              <div className="td col-size muted">{formatSize(f.total_size)}</div>
              <div className="td col-modified muted">{f.latest_modified ? formatDate(f.latest_modified) : "—"}</div>
              <div className="td col-actions actions-cell">
                {isAdmin && (
                  <button
                    className="btn-small btn-danger"
                    onClick={() => setConfirmPurge({ prefix: f.prefix, label: f.prefix.replace(/\/$/, "").split("/").pop() + "/" })}
                    title="Permanently delete all versions and delete markers"
                  >
                    Purge
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {confirmPurge && (
        <ConfirmDialog
          title="Purge All Versions"
          message={<>
            <p>Permanently delete <strong>all versions and delete markers</strong> for:</p>
            <p className="mono" style={{ fontSize: 12, wordBreak: "break-all" }}>{confirmPurge.label}</p>
            <p className="warning">This will permanently destroy all version history. This cannot be undone.</p>
          </>}
          confirmLabel="Purge"
          variant="danger"
          onConfirm={() => { const p = confirmPurge; setConfirmPurge(null); onPurge(p.keys || [], p.prefix || ""); }}
          onCancel={() => setConfirmPurge(null)}
        />
      )}
    </div>
  );
}
