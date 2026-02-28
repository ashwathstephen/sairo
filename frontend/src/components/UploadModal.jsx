import React, { useState, useRef, useEffect } from "react";
import { formatSize, getUploadUrl } from "../api";

// Threshold for batching: files under this size go in batches
const BATCH_SIZE_LIMIT = 5 * 1024 * 1024; // 5MB per batch
const BATCH_FILE_LIMIT = 10; // max files per batch request
const CONCURRENCY = 4;

function formatEta(seconds) {
  if (seconds < 60) return `${Math.ceil(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.ceil(seconds % 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m ${s}s`;
}

export default function UploadModal({ bucket, prefix, initialFiles, onClose, onUploaded }) {
  const [fileStates, setFileStates] = useState([]);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef();
  const abortRef = useRef(null);
  const taskStartTimes = useRef({});

  useEffect(() => {
    if (initialFiles && initialFiles.length > 0) {
      setFileStates(initialFiles.map(f => ({ file: f, status: "pending", progress: 0, eta: null })));
    }
  }, []);

  const addFiles = (newFiles) => {
    setFileStates(prev => [
      ...prev,
      ...newFiles.map(f => ({ file: f, status: "pending", progress: 0, eta: null })),
    ]);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragOver(false);
    addFiles([...e.dataTransfer.files]);
  };

  const removeFile = (idx) => {
    setFileStates(prev => prev.filter((_, i) => i !== idx));
  };

  const handleUpload = async () => {
    setUploading(true);
    let completed = 0;
    let errors = 0;
    const abortController = new AbortController();
    abortRef.current = abortController;

    // Separate large files (individual upload) from small files (batchable)
    const pending = [];
    for (let i = 0; i < fileStates.length; i++) {
      if (fileStates[i].status !== "complete" && fileStates[i].status !== "error") {
        pending.push(i);
      }
    }

    // Group small files into batches, keep large files individual
    const tasks = []; // each task is { indices: number[], files: File[] }
    let currentBatch = { indices: [], files: [], size: 0 };

    for (const i of pending) {
      const file = fileStates[i].file;
      if (file.size > BATCH_SIZE_LIMIT) {
        // Large file — upload individually for progress tracking
        tasks.push({ indices: [i], files: [file], large: true });
      } else {
        if (currentBatch.files.length >= BATCH_FILE_LIMIT ||
            currentBatch.size + file.size > BATCH_SIZE_LIMIT) {
          if (currentBatch.files.length > 0) tasks.push({ ...currentBatch, large: false });
          currentBatch = { indices: [], files: [], size: 0 };
        }
        currentBatch.indices.push(i);
        currentBatch.files.push(file);
        currentBatch.size += file.size;
      }
    }
    if (currentBatch.files.length > 0) tasks.push({ ...currentBatch, large: false });

    const uploadTask = async (task) => {
      if (abortController.signal.aborted) return;

      const taskKey = task.indices.join(",");
      taskStartTimes.current[taskKey] = Date.now();

      // Mark all files in this task as uploading
      setFileStates(prev => prev.map((fs, idx) =>
        task.indices.includes(idx) ? { ...fs, status: "uploading", progress: 0, eta: null } : fs
      ));

      try {
        const form = new FormData();
        form.append("prefix", prefix);
        for (const file of task.files) {
          form.append("files", file);
        }

        const xhr = new XMLHttpRequest();
        const promise = new Promise((resolve, reject) => {
          xhr.upload.addEventListener("progress", (e) => {
            if (e.lengthComputable) {
              const pct = (e.loaded / e.total) * 100;
              const elapsed = (Date.now() - taskStartTimes.current[taskKey]) / 1000;
              const bytesPerSec = elapsed > 0.5 ? e.loaded / elapsed : 0;
              const eta = bytesPerSec > 0 ? (e.total - e.loaded) / bytesPerSec : null;
              setFileStates(prev => prev.map((fs, idx) =>
                task.indices.includes(idx) ? { ...fs, progress: pct, eta } : fs
              ));
            }
          });
          xhr.addEventListener("load", () => {
            if (xhr.status >= 200 && xhr.status < 300) resolve();
            else reject(new Error(`Upload failed: ${xhr.status}`));
          });
          xhr.addEventListener("error", () => reject(new Error("Network error")));
          xhr.addEventListener("abort", () => {
            const err = new Error("Aborted");
            err.name = "AbortError";
            reject(err);
          });
        });

        abortController.signal.addEventListener("abort", () => xhr.abort());

        xhr.open("POST", getUploadUrl(bucket));
        xhr.withCredentials = true;
        xhr.send(form);

        await promise;
        completed += task.indices.length;
        setFileStates(prev => prev.map((fs, idx) =>
          task.indices.includes(idx) ? { ...fs, status: "complete", progress: 100, eta: null } : fs
        ));
      } catch (err) {
        if (err.name === "AbortError") return;
        errors += task.indices.length;
        setFileStates(prev => prev.map((fs, idx) =>
          task.indices.includes(idx) ? { ...fs, status: "error", progress: 0, eta: null } : fs
        ));
      }
    };

    // Run tasks with concurrency limit
    let cursor = 0;
    const runNext = async () => {
      while (cursor < tasks.length) {
        const task = tasks[cursor++];
        await uploadTask(task);
      }
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, tasks.length) }, () => runNext()));

    setUploading(false);
    abortRef.current = null;
    if (completed > 0 && errors === 0) {
      onUploaded();
    }
  };

  const pendingCount = fileStates.filter(fs => fs.status === "pending").length;
  const doneCount = fileStates.filter(fs => fs.status === "complete").length;
  const totalSize = fileStates.reduce((s, fs) => s + fs.file.size, 0);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()} style={{ width: 560 }}>
        <h2>Upload to {bucket}/{prefix || ""}</h2>

        <div
          className={`drop-zone ${dragOver ? "drag-over" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          onClick={() => inputRef.current.click()}
        >
          Drop files here or click to browse
        </div>
        <input
          ref={inputRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={(e) => addFiles([...e.target.files])}
        />

        {fileStates.length > 0 && (
          <>
            <div className="upload-summary">
              <span>{fileStates.length} file{fileStates.length !== 1 ? "s" : ""} ({formatSize(totalSize)})</span>
              {uploading && <span>{doneCount}/{fileStates.length} complete</span>}
            </div>
            <div style={{ maxHeight: 240, overflowY: "auto" }}>
              {fileStates.map((fs, i) => (
                <div key={i} className="upload-file-row">
                  <span className="upload-file-name">{fs.file.name}</span>
                  <span className="upload-file-size">{formatSize(fs.file.size)}</span>
                  <div className="upload-progress-bar">
                    <div
                      className={`upload-progress-fill ${fs.status === "complete" ? "complete" : ""} ${fs.status === "error" ? "error" : ""}`}
                      style={{ width: `${fs.progress}%` }}
                    />
                  </div>
                  <span className="upload-file-status">
                    {fs.status === "pending" && "\u2014"}
                    {fs.status === "uploading" && (
                      fs.progress >= 100
                        ? "Saving..."
                        : fs.eta != null && fs.eta > 1
                          ? `${Math.round(fs.progress)}% \u00b7 ${formatEta(fs.eta)}`
                          : `${Math.round(fs.progress)}%`
                    )}
                    {fs.status === "complete" && "\u2713"}
                    {fs.status === "error" && "\u2717"}
                  </span>
                  {!uploading && <button className="upload-file-cancel" onClick={() => removeFile(i)}>&times;</button>}
                </div>
              ))}
            </div>
          </>
        )}

        <div className="modal-actions">
          <button onClick={onClose} disabled={uploading}>Cancel</button>
          <button
            onClick={handleUpload}
            disabled={uploading || pendingCount === 0}
            className="btn-primary"
          >
            {uploading ? `Uploading... (${doneCount}/${fileStates.length})` : `Upload ${fileStates.length} file${fileStates.length !== 1 ? "s" : ""}`}
          </button>
        </div>
      </div>
    </div>
  );
}
