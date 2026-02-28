import React, { useState, useEffect } from "react";
import { getPresignedUrl, fetchPreview, fetchPreviewTail, fetchFileMetadata, formatSize } from "../api";

const IMAGE_EXTS = ["jpg", "jpeg", "png", "gif", "svg", "webp", "ico", "bmp"];
const TEXT_EXTS = ["txt", "md", "json", "csv", "xml", "yaml", "yml", "js", "jsx", "ts", "tsx", "py", "sql", "sh", "bash", "conf", "cfg", "ini", "env", "html", "css", "toml", "properties", "java", "go", "rs", "rb", "php", "c", "cpp", "h"];
const PDF_EXTS = ["pdf"];
const LOG_EXTS = ["log", "out", "err"];
const SCHEMA_EXTS = ["parquet", "orc", "avro"];

function getExt(key) {
  const dot = key.lastIndexOf(".");
  return dot >= 0 ? key.substring(dot + 1).toLowerCase() : "";
}

function getPreviewType(key) {
  const ext = getExt(key);
  if (IMAGE_EXTS.includes(ext)) return "image";
  if (PDF_EXTS.includes(ext)) return "pdf";
  if (SCHEMA_EXTS.includes(ext)) return "schema";
  if (LOG_EXTS.includes(ext)) return "log";
  if (TEXT_EXTS.includes(ext)) return "text";
  return "unsupported";
}

function parseCSV(text) {
  const lines = text.split("\n").filter(Boolean);
  if (lines.length === 0) return { headers: [], rows: [] };
  const headers = lines[0].split(",").map(h => h.trim().replace(/^"|"$/g, ""));
  const rows = lines.slice(1, 101).map(line => {
    const cols = [];
    let current = "";
    let inQuotes = false;
    for (const ch of line) {
      if (ch === '"') inQuotes = !inQuotes;
      else if (ch === "," && !inQuotes) { cols.push(current.trim()); current = ""; }
      else current += ch;
    }
    cols.push(current.trim());
    return cols;
  });
  return { headers, rows };
}

function SchemaPreview({ metadata }) {
  if (!metadata) return null;
  const fmt = metadata.format.toUpperCase();

  return (
    <div className="schema-preview">
      <div className="schema-header">
        <span className="schema-badge">{fmt}</span>
        <div className="schema-stats">
          {metadata.num_rows != null && <span>{metadata.num_rows.toLocaleString()} rows</span>}
          <span>{metadata.num_columns} columns</span>
          <span>{formatSize(metadata.file_size)}</span>
          {metadata.num_row_groups != null && <span>{metadata.num_row_groups} row group{metadata.num_row_groups !== 1 ? "s" : ""}</span>}
          {metadata.num_stripes != null && <span>{metadata.num_stripes} stripe{metadata.num_stripes !== 1 ? "s" : ""}</span>}
          {metadata.compression && <span>{metadata.compression}</span>}
        </div>
      </div>
      {metadata.created_by && <div className="schema-created-by">Created by: {metadata.created_by}</div>}
      {metadata.schema_name && <div className="schema-created-by">Schema: {metadata.namespace ? `${metadata.namespace}.` : ""}{metadata.schema_name}</div>}
      <table className="schema-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Column</th>
            <th>Type</th>
            <th>Nullable</th>
          </tr>
        </thead>
        <tbody>
          {metadata.columns.map((col, i) => (
            <tr key={i}>
              <td className="muted">{i + 1}</td>
              <td className="mono">{col.name}</td>
              <td className="schema-type">{col.type}</td>
              <td>{col.nullable ? "yes" : "no"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {metadata.row_groups && metadata.row_groups.length > 0 && (
        <details className="schema-row-groups">
          <summary>Row Groups ({metadata.row_groups.length})</summary>
          <table className="schema-table" style={{ marginTop: 8 }}>
            <thead>
              <tr><th>#</th><th>Rows</th><th>Size</th></tr>
            </thead>
            <tbody>
              {metadata.row_groups.map((rg, i) => (
                <tr key={i}>
                  <td className="muted">{i + 1}</td>
                  <td>{rg.num_rows.toLocaleString()}</td>
                  <td>{formatSize(rg.total_byte_size)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

function LogPreview({ content, truncated, showing, totalSize }) {
  if (!content) return null;
  const lines = content.split("\n");

  return (
    <div className="preview-text log-preview">
      {truncated && (
        <div className="log-truncated-banner">
          Showing {showing === "tail" ? "last" : "first"} portion of {formatSize(totalSize)}
        </div>
      )}
      <div className="preview-text-lines">
        {lines.map((line, i) => (
          <div key={i} className="preview-line">
            <span className="preview-line-num">{showing === "tail" ? "..." : i + 1}</span>
            <span className="preview-line-content">{line}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function FilePreview({ bucket, fileKey, size, onClose }) {
  const [url, setUrl] = useState(null);
  const [content, setContent] = useState(null);
  const [truncated, setTruncated] = useState(false);
  const [metadata, setMetadata] = useState(null);
  const [logData, setLogData] = useState(null);
  const [logMode, setLogMode] = useState("tail"); // "tail" or "head"
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const type = getPreviewType(fileKey);
  const ext = getExt(fileKey);
  const filename = fileKey.split("/").pop();

  useEffect(() => {
    setLoading(true);
    setError(null);
    setMetadata(null);
    setLogData(null);
    setContent(null);

    if (type === "image" || type === "pdf") {
      getPresignedUrl(bucket, fileKey, 3600).then(data => {
        setUrl(data.url);
        setLoading(false);
      }).catch(e => { setError(e.message); setLoading(false); });
    } else if (type === "schema") {
      fetchFileMetadata(bucket, fileKey).then(data => {
        setMetadata(data);
        setLoading(false);
      }).catch(e => { setError(e.message); setLoading(false); });
    } else if (type === "log") {
      // Log files: start with tail view
      fetchPreviewTail(bucket, fileKey).then(data => {
        setLogData(data);
        setLogMode("tail");
        setLoading(false);
      }).catch(e => { setError(e.message); setLoading(false); });
    } else if (type === "text") {
      const maxBytes = size > 5 * 1024 * 1024 ? 512000 : undefined;
      fetchPreview(bucket, fileKey, maxBytes).then(data => {
        setContent(data.content);
        setTruncated(data.truncated);
        setLoading(false);
      }).catch(e => { setError(e.message); setLoading(false); });
    } else {
      setLoading(false);
    }
  }, [bucket, fileKey, type, size]);

  const switchLogMode = (mode) => {
    setLoading(true);
    setError(null);
    if (mode === "tail") {
      fetchPreviewTail(bucket, fileKey).then(data => {
        setLogData(data);
        setLogMode("tail");
        setLoading(false);
      }).catch(e => { setError(e.message); setLoading(false); });
    } else {
      const maxBytes = size > 5 * 1024 * 1024 ? 512000 : undefined;
      fetchPreview(bucket, fileKey, maxBytes).then(data => {
        setLogData({ content: data.content, truncated: data.truncated, showing: "head", total_size: size });
        setLogMode("head");
        setLoading(false);
      }).catch(e => { setError(e.message); setLoading(false); });
    }
  };

  const renderContent = () => {
    if (loading) return <div className="empty"><div className="spinner" /> Loading preview...</div>;
    if (error) return <div className="empty" style={{ color: "var(--danger)" }}>Error: {error}</div>;

    if (type === "image") {
      return (
        <div className="preview-image">
          <img src={url} alt={filename} onError={() => setError("Failed to load image")} />
        </div>
      );
    }

    if (type === "pdf") {
      return <iframe src={url} className="preview-iframe" title={filename} />;
    }

    if (type === "schema" && metadata) {
      return <SchemaPreview metadata={metadata} />;
    }

    if (type === "log" && logData) {
      return <LogPreview content={logData.content} truncated={logData.truncated} showing={logData.showing || logMode} totalSize={logData.total_size || size} />;
    }

    if (type === "text" && content !== null) {
      // JSON formatting
      if (ext === "json") {
        try {
          const parsed = JSON.parse(content);
          const formatted = JSON.stringify(parsed, null, 2);
          const lines = formatted.split("\n");
          return (
            <div className="preview-text">
              <div className="preview-text-lines">
                {lines.map((line, i) => (
                  <div key={i} className="preview-line">
                    <span className="preview-line-num">{i + 1}</span>
                    <span className="preview-line-content">{line}</span>
                  </div>
                ))}
              </div>
            </div>
          );
        } catch {
          // Fall through to plain text
        }
      }

      // CSV as table
      if (ext === "csv") {
        const { headers, rows } = parseCSV(content);
        if (headers.length > 0) {
          return (
            <div style={{ overflow: "auto" }}>
              <table className="preview-csv-table">
                <thead>
                  <tr>{headers.map((h, i) => <th key={i}>{h}</th>)}</tr>
                </thead>
                <tbody>
                  {rows.map((row, i) => (
                    <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
      }

      // Plain text with line numbers
      const lines = content.split("\n");
      return (
        <div className="preview-text">
          <div className="preview-text-lines">
            {lines.map((line, i) => (
              <div key={i} className="preview-line">
                <span className="preview-line-num">{i + 1}</span>
                <span className="preview-line-content">{line}</span>
              </div>
            ))}
          </div>
        </div>
      );
    }

    // Unsupported
    return (
      <div className="preview-unsupported">
        <div className="preview-unsupported-icon">&#128196;</div>
        <p>Preview not available for .{ext} files</p>
        <button className="btn-primary" onClick={() => {
          getPresignedUrl(bucket, fileKey, 3600).then(data => {
            window.open(data.url, "_blank");
          });
        }}>Download file ({formatSize(size)})</button>
      </div>
    );
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal preview-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="preview-header">
          <h2 title={fileKey}>{filename}</h2>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {type === "log" && !loading && (
              <div className="log-mode-toggle">
                <button className={`log-mode-btn ${logMode === "tail" ? "log-mode-active" : ""}`} onClick={() => switchLogMode("tail")}>Tail</button>
                <button className={`log-mode-btn ${logMode === "head" ? "log-mode-active" : ""}`} onClick={() => switchLogMode("head")}>Head</button>
              </div>
            )}
            <span className="muted" style={{ fontSize: 12 }}>{formatSize(size)}</span>
            <button onClick={onClose} className="btn-settings" style={{ padding: "4px 8px" }}>&times;</button>
          </div>
        </div>
        {truncated && type === "text" && <div className="preview-truncated">Showing first 500 KB of {formatSize(size)}</div>}
        <div className="preview-body">
          {renderContent()}
        </div>
      </div>
    </div>
  );
}
