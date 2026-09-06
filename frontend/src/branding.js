// Apply a white-label PRIMARY_COLOR to the accent CSS variables (main accent, hover, focus ring) so a
// branded deployment is recoloured throughout the UI — the main app and the public share page alike.
export function applyAccent(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return;
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16));
  const dark = (v, f) => Math.max(0, Math.round(v * (1 - f)));
  const root = document.documentElement.style;
  root.setProperty("--primary", `#${m[1]}`);
  root.setProperty("--primary-hover", `rgb(${dark(r, .12)}, ${dark(g, .12)}, ${dark(b, .12)})`);
  root.setProperty("--primary-ring", `rgba(${r}, ${g}, ${b}, 0.3)`);
}
