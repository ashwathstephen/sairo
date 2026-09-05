import React, { useState, useEffect } from "react";
import { getCrawlStatus, triggerCrawl, formatSize } from "../api";

export default function CrawlStatus({ bucket }) {
  const [status, setStatus] = useState(null);
  const [expanded, setExpanded] = useState(false);
  const [starting, setStarting] = useState(false);

  const fetchStatus = async () => {
    try {
      const data = await getCrawlStatus(bucket);
      setStatus(data);
    } catch {}
  };

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, [bucket]);

  if (!status) return null;

  const isInterrupted = status.status === "interrupted";
  const isCrawling = status.status === "crawling" || isInterrupted;
  const isDegraded = status.status === "degraded";           // index served, some folders failed to list
  const isComplete = status.status === "complete" || isDegraded;
  const isRebuilding = isComplete && status.rebuilding;       // rows in, search index still being rebuilt
  const isError = status.status?.startsWith("error");

  const handleRecrawl = async () => {
    if (starting || isCrawling) return;       // guard against the double-click
    setStarting(true);
    try {
      await triggerCrawl(bucket);
      // Optimistically reflect "crawling" right away — the crawl_status table lags a beat
      // behind the trigger, which is what made the first click look like a no-op.
      setStatus((s) => ({ ...(s || {}), status: "crawling" }));
      // Poll quickly for a few seconds to sync the real status, then the 5s interval takes over.
      for (let i = 0; i < 6; i++) {
        await new Promise((r) => setTimeout(r, 800));
        await fetchStatus();
      }
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="crawl-status-wrapper">
      <button
        className={`crawl-badge ${isCrawling ? "crawl-active" : isComplete ? "crawl-complete" : isError ? "crawl-error" : "crawl-idle"}`}
        onClick={() => setExpanded(!expanded)}
        title="Index status"
      >
        {isCrawling && <span className="crawl-dot" />}
        {isCrawling
          ? `${isInterrupted ? "Resuming index..." : "Indexing..."} ${(status.total_objects || 0).toLocaleString()}`
          : isComplete
          ? `${(status.total_objects || 0).toLocaleString()} objects${isRebuilding ? " · finishing search index" : isDegraded ? " · partial" : ""}`
          : isError ? "Index error" : "Not indexed"}
      </button>

      {expanded && (
        <div className="crawl-dropdown">
          <div className="crawl-detail">
            <span className="crawl-label">Status</span>
            <span>{isRebuilding ? `${status.status} (rebuilding search index)` : status.status}</span>
          </div>
          {status.last_error && (
            <div className="crawl-detail">
              <span className="crawl-label">Last error</span>
              <span>{status.last_error}</span>
            </div>
          )}
          <div className="crawl-detail">
            <span className="crawl-label">Objects</span>
            <span>{(status.total_objects || 0).toLocaleString()}</span>
          </div>
          <div className="crawl-detail">
            <span className="crawl-label">Total Size</span>
            <span>{formatSize(status.total_size || 0)}</span>
          </div>
          {status.last_crawl_end && (
            <div className="crawl-detail">
              <span className="crawl-label">Last Indexed</span>
              <span>{new Date(status.last_crawl_end).toLocaleString()}</span>
            </div>
          )}
          <button onClick={handleRecrawl} disabled={isCrawling || starting} className="btn-primary" style={{ marginTop: 8, width: "100%" }}>
            {starting ? "Starting…" : isCrawling ? "Indexing…" : "Re-index Now"}
          </button>
        </div>
      )}
    </div>
  );
}
