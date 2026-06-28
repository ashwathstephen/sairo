import React, { useState, useRef, useEffect } from "react";
import { searchObjects, getCrawlStatus, formatSize, formatDate, downloadUrl } from "../api";

function highlightMatch(text, query) {
  if (!query || query.length < 2) return text;
  const lower = text.toLowerCase();
  const idx = lower.indexOf(query.toLowerCase());
  if (idx === -1) return text;
  return (
    <>
      {text.substring(0, idx)}
      <mark className="search-highlight">{text.substring(idx, idx + query.length)}</mark>
      {text.substring(idx + query.length)}
    </>
  );
}

export default function SearchBar({ bucket, prefix, onClose, onNavigate, onFileInfo }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [indexing, setIndexing] = useState(null); // {objects} while the index is still building, else null
  const [selectedIdx, setSelectedIdx] = useState(-1);
  const inputRef = useRef();
  const timerRef = useRef();
  const retryRef = useRef();
  const listRef = useRef();

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!query || query.length < 2) {
      setResults(null);
      setIndexing(null);
      return;
    }
    clearTimeout(timerRef.current);
    clearTimeout(retryRef.current);
    let cancelled = false;
    const run = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await searchObjects(bucket, query, prefix);
        if (cancelled) return;
        setResults(data);
        setIndexing(null);
        setSelectedIdx(-1);
      } catch (e) {
        if (cancelled) return;
        if (e.message.includes("503")) {
          // Index still building — reassure + show progress, and re-run automatically
          // once the crawl is ready (turns the index-not-ready race into a smooth wait).
          setResults(null);
          try {
            const s = await getCrawlStatus(bucket);
            if (!cancelled) setIndexing({ objects: s.total_objects || 0 });
          } catch {
            if (!cancelled) setIndexing({ objects: 0 });
          }
          retryRef.current = setTimeout(() => { if (!cancelled) run(); }, 2500);
        } else {
          setError(e.message);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    timerRef.current = setTimeout(run, 300);
    return () => { cancelled = true; clearTimeout(timerRef.current); clearTimeout(retryRef.current); };
  }, [query, prefix, bucket]);

  const scrollToItem = (idx) => {
    const el = listRef.current?.children[idx];
    if (el) el.scrollIntoView({ block: "nearest" });
  };

  const navigateToFolder = (key) => {
    const idx = key.lastIndexOf("/");
    const folder = idx >= 0 ? key.substring(0, idx + 1) : "";
    onClose();
    onNavigate(folder);
  };

  const handleKeyDown = (e) => {
    if (e.key === "Escape") { onClose(); return; }

    const items = results?.results;
    if (!items || items.length === 0) return;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIdx((prev) => {
        const next = prev < items.length - 1 ? prev + 1 : 0;
        scrollToItem(next);
        return next;
      });
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIdx((prev) => {
        const next = prev > 0 ? prev - 1 : items.length - 1;
        scrollToItem(next);
        return next;
      });
    } else if (e.key === "Enter" && selectedIdx >= 0 && selectedIdx < items.length) {
      e.preventDefault();
      const r = items[selectedIdx];
      if (e.metaKey || e.ctrlKey) {
        // Cmd/Ctrl+Enter: download
        window.location.href = downloadUrl(bucket, r.key);
      } else {
        // Enter: navigate to containing folder
        navigateToFolder(r.key);
      }
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal modal-wide search-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="search-header">
          <span className="search-icon">&#128269;</span>
          <input
            ref={inputRef}
            type="text"
            className="search-input"
            placeholder={prefix ? `Search in ${prefix}...` : `Search ${bucket}...`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          {results?.results?.length > 0 && <kbd className="kbd">&uarr;&darr;</kbd>}
          <kbd className="kbd">ESC</kbd>
        </div>

        <div className="search-results">
          {loading && !indexing && (
            <div className="search-loading"><div className="spinner" /> Searching...</div>
          )}
          {indexing && (
            <div className="search-indexing">
              <div className="spinner" />
              <div className="search-indexing-text">
                Indexing your bucket{indexing.objects > 0 ? ` — ${indexing.objects.toLocaleString()} objects so far` : "…"}
                <div className="search-indexing-sub">Hang tight — your search will run automatically the moment the index is ready.</div>
              </div>
            </div>
          )}
          {error && <div className="search-error">{error}</div>}
          {results && !loading && (
            <>
              <div className="search-count">{results.count} result{results.count !== 1 ? "s" : ""}</div>
              {results.results.length === 0 ? (
                <div className="search-empty">No objects found matching &ldquo;{query}&rdquo;</div>
              ) : (
                <ul className="search-list" ref={listRef}>
                  {results.results.map((r, i) => {
                    const parts = r.key.split("/");
                    const name = parts.pop();
                    const path = parts.join("/") + (parts.length > 0 ? "/" : "");
                    return (
                      <li
                        key={r.key}
                        className={`search-item ${i === selectedIdx ? "search-item-active" : ""}`}
                        onMouseEnter={() => setSelectedIdx(i)}
                      >
                        <div className="search-item-main">
                          <span className="search-item-name" title={r.key}>{highlightMatch(name, query)}</span>
                          <span className="search-item-size">{formatSize(r.size)}</span>
                        </div>
                        <div className="search-item-path">
                          <a href="#" onClick={(e) => { e.preventDefault(); navigateToFolder(r.key); }}>{path || "/"}</a>
                          <span className="search-item-date">{formatDate(r.last_modified)}</span>
                        </div>
                        <div className="search-item-actions">
                          <a href={downloadUrl(bucket, r.key)} className="btn-small btn-xs">&#8595;</a>
                          <button className="btn-small btn-xs btn-info" onClick={() => { onClose(); onFileInfo(r.key); }}>i</button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          )}
          {!results && !loading && !error && (
            <div className="search-hint">Type at least 2 characters to search across all objects in the bucket</div>
          )}
        </div>
      </div>
    </div>
  );
}
