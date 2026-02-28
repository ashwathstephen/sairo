import React, { useState, useEffect, useRef } from "react";
import { getCurrentEndpoint } from "../api";

function loadFavorites() {
  try {
    const favs = JSON.parse(localStorage.getItem("s3-browser-favorites") || "[]");
    // Migration: add endpoint field if missing (defaults to "default")
    return favs.map(f => ({ ...f, endpoint: f.endpoint || "default" }));
  } catch { return []; }
}

function saveFavorites(favs) {
  localStorage.setItem("s3-browser-favorites", JSON.stringify(favs));
}

export default function Favorites({ onNavigate, currentBucket, currentPrefix }) {
  const [open, setOpen] = useState(false);
  const [favorites, setFavorites] = useState(loadFavorites);
  const wrapperRef = useRef();

  const currentEndpoint = getCurrentEndpoint() || "default";

  useEffect(() => {
    const handler = (e) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const isFavorite = currentBucket && favorites.some(f =>
    f.bucket === currentBucket &&
    f.prefix === (currentPrefix || "") &&
    (f.endpoint || "default") === currentEndpoint
  );

  const toggleCurrent = (e) => {
    e.stopPropagation();
    if (!currentBucket) return;
    const updated = isFavorite
      ? favorites.filter(f => !(f.bucket === currentBucket && f.prefix === (currentPrefix || "") && (f.endpoint || "default") === currentEndpoint))
      : [...favorites, {
          bucket: currentBucket,
          prefix: currentPrefix || "",
          label: currentPrefix ? currentPrefix.split("/").filter(Boolean).pop() : currentBucket,
          endpoint: currentEndpoint,
        }];
    setFavorites(updated);
    saveFavorites(updated);
  };

  const removeFavorite = (idx, e) => {
    e.stopPropagation();
    const updated = favorites.filter((_, i) => i !== idx);
    setFavorites(updated);
    saveFavorites(updated);
  };

  return (
    <div className="favorites-wrapper" ref={wrapperRef}>
      <button
        className={`favorites-btn ${favorites.length > 0 ? "has-favorites" : ""}`}
        onClick={() => setOpen(!open)}
        title="Favorites"
      >
        &#9733;
      </button>
      {open && (
        <div className="favorites-dropdown">
          <div className="favorites-header">
            <span>Favorites</span>
            {currentBucket && (
              <button className={`btn-favorite ${isFavorite ? "is-favorite" : ""}`} onClick={toggleCurrent} title={isFavorite ? "Remove from favorites" : "Add current path"}>
                {isFavorite ? "\u2605" : "\u2606"}
              </button>
            )}
          </div>
          {favorites.length === 0 ? (
            <div className="favorites-empty">No favorites yet. Navigate to a path and click the star to bookmark it.</div>
          ) : (
            favorites.map((fav, i) => (
              <div key={i} className="favorites-item" onClick={() => { setOpen(false); onNavigate(fav.bucket, fav.prefix, fav.endpoint || "default"); }}>
                <span className="favorites-item-icon">&#128230;</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="favorites-item-text">{fav.prefix || fav.bucket}</div>
                  <div className="favorites-item-bucket">
                    {fav.prefix ? fav.bucket : ""}
                    {fav.endpoint && fav.endpoint !== "default" && (
                      <span className="ep-inline-badge" style={{ marginLeft: 4 }}>{fav.endpoint}</span>
                    )}
                  </div>
                </div>
                <button className="favorites-item-remove" onClick={(e) => removeFavorite(i, e)} title="Remove">&times;</button>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
