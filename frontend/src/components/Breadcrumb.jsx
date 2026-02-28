import React from "react";

export default function Breadcrumb({ bucket, prefix, onNavigate, onHome, isFavorite, onToggleFavorite }) {
  const parts = prefix.split("/").filter(Boolean);

  const crumbs = [{ label: bucket, prefix: "" }];
  let acc = "";
  for (const part of parts) {
    acc += part + "/";
    crumbs.push({ label: part, prefix: acc });
  }

  return (
    <nav className="breadcrumb">
      <a href="#" onClick={(e) => { e.preventDefault(); onHome(); }} style={{ marginRight: 4 }}>
        Buckets
      </a>
      <span className="separator">/</span>
      {crumbs.map((c, i) => (
        <span key={c.prefix || "root"}>
          {i > 0 && <span className="separator">/</span>}
          {i < crumbs.length - 1 ? (
            <a
              href="#"
              onClick={(e) => { e.preventDefault(); onNavigate(c.prefix); }}
            >
              {c.label}
            </a>
          ) : (
            <span className="current">{c.label}</span>
          )}
        </span>
      ))}
      {onToggleFavorite && (
        <button
          className={`btn-favorite ${isFavorite ? "is-favorite" : ""}`}
          onClick={onToggleFavorite}
          title={isFavorite ? "Remove from favorites" : "Add to favorites"}
          style={{ marginLeft: 6 }}
        >
          {isFavorite ? "\u2605" : "\u2606"}
        </button>
      )}
    </nav>
  );
}
