import React from "react";

const TIPS = [
  { icon: "\uD83D\uDD0D", title: "Search", desc: "Press / to search across all files in a bucket. Supports wildcards." },
  { icon: "\u2328\uFE0F", title: "Shortcuts", desc: "Backspace to go up, Esc to go back, ? for help." },
  { icon: "\uD83D\uDCE4", title: "Drag & Drop", desc: "Drag files anywhere to upload them to the current folder." },
  { icon: "\uD83D\uDCC4", title: "Preview", desc: "Click the eye icon to preview images, text, CSV, JSON, and Parquet schemas." },
];

export default function Welcome({ onDismiss }) {
  const handleDismiss = () => {
    localStorage.setItem("sairo-onboarded", "1");
    onDismiss();
  };

  return (
    <div className="modal-overlay" onClick={handleDismiss}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 480 }}>
        <h2>Welcome to Sairo</h2>
        <p style={{ color: "var(--text-muted)", marginBottom: 16 }}>Here are a few tips to get you started:</p>
        <div className="welcome-tips">
          {TIPS.map((t, i) => (
            <div key={i} className="welcome-tip">
              <span className="welcome-tip-icon">{t.icon}</span>
              <div>
                <strong>{t.title}</strong>
                <p>{t.desc}</p>
              </div>
            </div>
          ))}
        </div>
        <div className="modal-actions">
          <button onClick={handleDismiss} className="btn-primary">Got it</button>
        </div>
      </div>
    </div>
  );
}
