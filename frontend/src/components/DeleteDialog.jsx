import React, { useState } from "react";

export default function DeleteDialog({ count, folderCount = 0, fileKeys = [], folderPrefixes = [], isAdmin = false, onConfirm, onCancel }) {
  const [purgeAll, setPurgeAll] = useState(false);

  const parts = [];
  if (count > 0) parts.push(`${count} file${count !== 1 ? "s" : ""}`);
  if (folderCount > 0) parts.push(`${folderCount} folder${folderCount !== 1 ? "s" : ""} (and all contents)`);
  const summary = parts.join(" and ") || "selected items";
  const hasItems = fileKeys.length > 0 || folderPrefixes.length > 0;

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal modal-small" role="alertdialog" aria-modal="true" aria-labelledby="delete-dialog-title" onClick={(e) => e.stopPropagation()}>
        <h2 id="delete-dialog-title">Confirm Delete</h2>
        <p>Are you sure you want to delete {summary}?</p>
        {hasItems && (
          <ul className="delete-file-list">
            {folderPrefixes.map(p => <li key={p}>&#128193; {p.split("/").filter(Boolean).pop()}/</li>)}
            {fileKeys.map(k => <li key={k}>{k.split("/").pop()}</li>)}
          </ul>
        )}
        {folderCount > 0 && <p className="warning">Folder deletion will remove ALL objects inside the folder recursively.</p>}
        {isAdmin && (
          <label className="purge-checkbox">
            <input type="checkbox" checked={purgeAll} onChange={(e) => setPurgeAll(e.target.checked)} />
            <span>Permanently delete all versions</span>
            {purgeAll && <p className="warning" style={{ margin: "4px 0 0 0" }}>This will permanently destroy all version history. This cannot be undone.</p>}
          </label>
        )}
        {!purgeAll && <p className="warning">This action cannot be undone.</p>}
        <div className="modal-actions">
          <button onClick={onCancel}>Cancel</button>
          <button onClick={() => onConfirm({ purgeVersions: purgeAll })} className="btn-danger">
            {purgeAll ? "Purge & Delete" : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}
