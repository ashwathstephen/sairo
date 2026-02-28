import React, { useState, useEffect } from "react";
import { getCrawlStatus, triggerCrawl, formatSize } from "../api";

export default function CrawlStatus({ bucket }) {
  const [status, setStatus] = useState(null);
  const [expanded, setExpanded] = useState(false);

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

  const isCrawling = status.status === "crawling";
  const isComplete = status.status === "complete";
  const isError = status.status?.startsWith("error");

  const handleRecrawl = async () => {
    await triggerCrawl(bucket);
    fetchStatus();
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
          ? `Indexing... ${(status.total_objects || 0).toLocaleString()}`
          : isComplete
          ? `${(status.total_objects || 0).toLocaleString()} objects`
          : isError ? "Index error" : "Not indexed"}
      </button>

      {expanded && (
        <div className="crawl-dropdown">
          <div className="crawl-detail">
            <span className="crawl-label">Status</span>
            <span>{status.status}</span>
          </div>
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
          <button onClick={handleRecrawl} disabled={isCrawling} className="btn-primary" style={{ marginTop: 8, width: "100%" }}>
            {isCrawling ? "Crawling..." : "Re-index Now"}
          </button>
        </div>
      )}
    </div>
  );
}
