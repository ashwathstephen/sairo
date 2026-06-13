import React, { useState, useRef, useEffect } from "react";
import { formatSize, getPresignedUploadUrls, notifyUpload, getUploadUrl,
  multipartInitiate, multipartSign, multipartComplete, multipartAbort } from "../api";

const CONCURRENCY = 4;                           // files uploaded in parallel
const MULTIPART_THRESHOLD = 100 * 1024 * 1024;   // files larger than this use multipart
const PART_SIZE_MIN = 16 * 1024 * 1024;          // 16 MB base part size
const PART_CONCURRENCY = 4;                      // parts uploaded in parallel within one file
const PART_RETRIES = 3;                          // per-part retry attempts (resumable within session)

function computePartSize(fileSize) {
  let size = PART_SIZE_MIN;
  while (Math.ceil(fileSize / size) > 10000) size *= 2;  // S3 allows at most 10,000 parts
  return size;
}

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
  const [directMode, setDirectMode] = useState(true); // presigned PUT (default) vs proxy
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

  // ── Direct upload (no file data through Sairo) ──
  // Small files: single presigned PUT. Large files: parallel, resumable multipart
  // (parts PUT directly to S3) — no proxy buffering, no size ceiling, no pod OOM.
  const handleDirectUpload = async () => {
    setUploading(true);
    let completed = 0;
    let errors = 0;
    const abortController = new AbortController();
    abortRef.current = abortController;

    const pending = fileStates
      .map((fs, i) => ({ fs, i }))
      .filter(({ fs }) => fs.status !== "complete" && fs.status !== "error");

    const setFs = (i, patch) =>
      setFileStates(prev => prev.map((s, idx) => (idx === i ? { ...s, ...patch } : s)));

    // Presigned single-PUT URLs for the small files (batched).
    const smallKeys = pending.filter(({ fs }) => fs.file.size <= MULTIPART_THRESHOLD).map(({ fs }) => fs.file.name);
    const urlMap = {};
    try {
      if (smallKeys.length) {
        const resp = await getPresignedUploadUrls(bucket, smallKeys, prefix);
        for (const entry of resp.urls) urlMap[entry.key] = entry.url;
      }
    } catch (err) {
      // Presigned upload not available — fall back to proxy upload.
      setDirectMode(false);
      setUploading(false);
      return;
    }

    // PUT a body to a presigned URL with progress; resolves with the xhr (for ETag).
    const putWithProgress = (url, body, onLoaded) => new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.upload.addEventListener("progress", (e) => { if (e.lengthComputable) onLoaded(e.loaded); });
      xhr.addEventListener("load", () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve(xhr);
        else reject(new Error(`S3 upload failed: ${xhr.status}`));
      });
      xhr.addEventListener("error", () => reject(new Error("Network error")));
      xhr.addEventListener("abort", () => { const er = new Error("Aborted"); er.name = "AbortError"; reject(er); });
      abortController.signal.addEventListener("abort", () => xhr.abort());
      xhr.open("PUT", url);
      xhr.send(body);
    });

    const uploadSingle = async ({ fs, i }) => {
      const key = prefix + fs.file.name;
      const url = urlMap[key];
      if (!url) throw new Error("No presigned URL");
      taskStartTimes.current[i] = Date.now();
      setFs(i, { status: "uploading", progress: 0, eta: null });
      await putWithProgress(url, fs.file, (loaded) => {
        const elapsed = (Date.now() - taskStartTimes.current[i]) / 1000;
        const bps = elapsed > 0.5 ? loaded / elapsed : 0;
        setFs(i, { progress: (loaded / fs.file.size) * 100, eta: bps > 0 ? (fs.file.size - loaded) / bps : null });
      });
      await notifyUpload(bucket, [{ key, size: fs.file.size }]).catch(() => {});
    };

    const uploadMultipartFile = async ({ fs, i }) => {
      const file = fs.file;
      const partSize = computePartSize(file.size);
      const numParts = Math.ceil(file.size / partSize);
      taskStartTimes.current[i] = Date.now();
      setFs(i, { status: "uploading", progress: 0, eta: null });

      const { key, upload_id: uploadId } = await multipartInitiate(bucket, file.name, prefix, file.type || "");
      try {
        // Presign all parts (batched: the sign endpoint accepts up to 1000 at a time).
        const urlByPart = {};
        for (let start = 1; start <= numParts; start += 1000) {
          const nums = [];
          for (let p = start; p < start + 1000 && p <= numParts; p++) nums.push(p);
          const { urls } = await multipartSign(bucket, key, uploadId, nums);
          for (const u of urls) urlByPart[u.part_number] = u.url;
        }

        const loadedPerPart = new Array(numParts + 1).fill(0);
        const refreshProgress = () => {
          const loaded = loadedPerPart.reduce((a, b) => a + b, 0);
          const elapsed = (Date.now() - taskStartTimes.current[i]) / 1000;
          const bps = elapsed > 0.5 ? loaded / elapsed : 0;
          setFs(i, { progress: (loaded / file.size) * 100, eta: bps > 0 ? (file.size - loaded) / bps : null });
        };

        const etags = new Array(numParts + 1);
        let nextPart = 1;
        const worker = async () => {
          while (nextPart <= numParts) {
            if (abortController.signal.aborted) throw Object.assign(new Error("Aborted"), { name: "AbortError" });
            const part = nextPart++;
            const startByte = (part - 1) * partSize;
            const blob = file.slice(startByte, Math.min(startByte + partSize, file.size));
            let attempt = 0;
            for (;;) {
              try {
                const xhr = await putWithProgress(urlByPart[part], blob, (loaded) => { loadedPerPart[part] = loaded; refreshProgress(); });
                const etag = xhr.getResponseHeader("ETag");
                if (!etag) throw new Error("Missing ETag on part (check S3 CORS ExposeHeaders)");
                etags[part] = etag;
                break;
              } catch (e) {
                if (e.name === "AbortError") throw e;
                if (++attempt > PART_RETRIES) throw e;
                loadedPerPart[part] = 0; refreshProgress();
                await new Promise(r => setTimeout(r, 500 * attempt));  // backoff, then retry this part
              }
            }
          }
        };
        await Promise.all(Array.from({ length: Math.min(PART_CONCURRENCY, numParts) }, () => worker()));

        const parts = [];
        for (let p = 1; p <= numParts; p++) parts.push({ PartNumber: p, ETag: etags[p] });
        await multipartComplete(bucket, key, uploadId, parts);
        await notifyUpload(bucket, [{ key, size: file.size }]).catch(() => {});
      } catch (e) {
        await multipartAbort(bucket, key, uploadId);  // clean up the partial upload on S3
        throw e;
      }
    };

    const uploadOne = async ({ fs, i }) => {
      if (abortController.signal.aborted) return;
      try {
        if (fs.file.size > MULTIPART_THRESHOLD) await uploadMultipartFile({ fs, i });
        else await uploadSingle({ fs, i });
        completed++;
        setFs(i, { status: "complete", progress: 100, eta: null });
      } catch (err) {
        if (err.name === "AbortError") return;
        errors++;
        setFs(i, { status: "error", progress: 0, eta: null });
      }
    };

    // File-level concurrency.
    let cursor = 0;
    const runNext = async () => {
      while (cursor < pending.length) await uploadOne(pending[cursor++]);
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, pending.length) }, () => runNext()));

    setUploading(false);
    abortRef.current = null;
    if (completed > 0 && errors === 0) onUploaded();
  };

  // ── Proxy upload (legacy — files go through Sairo server) ──
  const handleProxyUpload = async () => {
    setUploading(true);
    let completed = 0;
    let errors = 0;
    const abortController = new AbortController();
    abortRef.current = abortController;

    const pending = [];
    for (let i = 0; i < fileStates.length; i++) {
      if (fileStates[i].status !== "complete" && fileStates[i].status !== "error") {
        pending.push(i);
      }
    }

    // Group small files into batches, keep large files individual
    const BATCH_SIZE_LIMIT = 5 * 1024 * 1024;
    const BATCH_FILE_LIMIT = 10;
    const tasks = [];
    let currentBatch = { indices: [], files: [], size: 0 };

    for (const i of pending) {
      const file = fileStates[i].file;
      if (file.size > BATCH_SIZE_LIMIT) {
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
      setFileStates(prev => prev.map((fs, idx) =>
        task.indices.includes(idx) ? { ...fs, status: "uploading", progress: 0, eta: null } : fs
      ));

      try {
        const form = new FormData();
        form.append("prefix", prefix);
        for (const file of task.files) form.append("files", file);

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
          xhr.addEventListener("abort", () => { const e = new Error("Aborted"); e.name = "AbortError"; reject(e); });
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
    if (completed > 0 && errors === 0) onUploaded();
  };

  const handleUpload = directMode ? handleDirectUpload : handleProxyUpload;

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
              {!uploading && (
                <label style={{ fontSize: 11, color: "var(--text-muted)", cursor: "pointer" }}>
                  <input type="checkbox" checked={directMode} onChange={(e) => setDirectMode(e.target.checked)} style={{ marginRight: 4 }} />
                  Direct upload
                </label>
              )}
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
