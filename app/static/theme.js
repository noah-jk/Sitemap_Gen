// Theme switch: Auto (follow the browser), Light, or Dark, remembered in this browser.
// Loaded in <head> without defer so the right colors apply before the page paints.
(function () {
  "use strict";
  const KEY = "sitemap-gen-theme";
  const ORDER = ["auto", "light", "dark"];
  const NAMES = {auto: "☀☾ Auto", light: "☀ Light", dark: "☾ Dark"};
  const root = document.documentElement;
  function read() {
    try { const v = localStorage.getItem(KEY); return ORDER.includes(v) ? v : "auto"; } catch (e) { return "auto"; }
  }
  function apply(theme) {
    if (theme === "auto") delete root.dataset.theme; else root.dataset.theme = theme;
  }
  apply(read());
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("theme-btn");
    if (!btn) return;
    const label = (t) => {
      btn.textContent = NAMES[t];
      btn.title = `Theme: ${t === "auto" ? "follows your device" : t}. Click to change.`;
    };
    label(read());
    btn.addEventListener("click", () => {
      const next = ORDER[(ORDER.indexOf(read()) + 1) % ORDER.length];
      try { localStorage.setItem(KEY, next); } catch (e) { /* private mode: applies to this page only */ }
      apply(next);
      label(next);
    });
  });
  // Another tab changed the theme.
  window.addEventListener("storage", (e) => { if (e.key === KEY) apply(read()); });
})();
