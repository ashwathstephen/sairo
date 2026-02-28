import React, { useState, useEffect } from "react";
import { Rows3, Rows4 } from "lucide-react";

export default function DensityToggle() {
  const [compact, setCompact] = useState(() => {
    return localStorage.getItem("density") === "compact";
  });

  useEffect(() => {
    document.documentElement.dataset.density = compact ? "compact" : "default";
    localStorage.setItem("density", compact ? "compact" : "default");
    window.dispatchEvent(new Event("density-change"));
  }, [compact]);

  return (
    <button
      className="btn-settings density-toggle"
      onClick={() => setCompact(!compact)}
      title={compact ? "Comfortable view" : "Compact view"}
      aria-label={compact ? "Switch to comfortable view" : "Switch to compact view"}
    >
      {compact ? <Rows3 size={16} /> : <Rows4 size={16} />}
    </button>
  );
}
