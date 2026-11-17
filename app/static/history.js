// Undo, redo, and the History list in the top bar. After an undo or redo, the page reloads its own
// data if it listens for "history:changed" (and calls preventDefault); otherwise the page reloads.
(function () {
  "use strict";
  const box = document.getElementById("history");
  if (!box) return;
  const slug = encodeURIComponent(box.dataset.slug);
  const csrf = box.dataset.csrf;
  const api = (path) => `/api/p/${slug}/history${path}`;
  const undoBtn = document.getElementById("undo-btn");
  const redoBtn = document.getElementById("redo-btn");
  const menu = document.getElementById("history-menu");
  const list = document.getElementById("history-list");
  const toast = document.getElementById("history-toast");
  let busy = false;
  let toastTimer = null;

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
    });
    children.forEach((c) => node.append(c));
    return node;
  }

  function ago(ms) {
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return new Date(ms).toLocaleDateString();
  }

  function show(data) {
    undoBtn.disabled = !data.undo;
    redoBtn.disabled = !data.redo;
    undoBtn.title = data.undo ? `Undo: ${data.undo} (Ctrl+Z)` : "Nothing to undo";
    redoBtn.title = data.redo ? `Redo: ${data.redo} (Ctrl+Shift+Z)` : "Nothing to redo";
    list.replaceChildren(...(data.actions.length ? data.actions.map((a) => el("li", {class: a.undone ? "undone" : ""},
      el("span", {class: "history-label", text: a.label}),
      el("span", {class: "muted small", text: `${a.user} · ${ago(a.created_at)}${a.undone ? " · undone" : ""}`})))
      : [el("li", {class: "muted small", text: "No edits yet. Changes you make show up here."})]));
  }

  async function refresh() {
    try {
      const resp = await fetch(api(""));
      if (resp.ok) show(await resp.json());
    } catch (e) { /* offline: leave the buttons as they are */ }
  }

  // A short message at the bottom of the screen; after an edit it carries an Undo button.
  function say(text, isError, offerUndo) {
    toast.replaceChildren(el("span", {text}));
    if (offerUndo) {
      const b = el("button", {type: "button", class: "toast-undo", text: "Undo"});
      b.addEventListener("click", () => { toast.hidden = true; step("undo"); });
      toast.append(b);
    }
    toast.classList.toggle("error", !!isError);
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, offerUndo ? 10000 : 6000);
  }

  async function step(which) {
    if (busy) return;
    busy = true;
    try {
      const resp = await fetch(api(`/${which}`), {method: "POST", headers: {"X-CSRF-Token": csrf}});
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        say(data.detail || "That didn't work. Reload the page and try again.", true);
        refresh();
        return;
      }
      show(data);
      say(`${which === "undo" ? "Undid" : "Redid"}: ${data.label}`);
      const ev = new CustomEvent("history:changed", {cancelable: true});
      if (document.dispatchEvent(ev)) location.reload();
    } catch (e) {
      say("Couldn't reach the server. Nothing was changed.", true);
    } finally {
      busy = false;
    }
  }

  // Tabs call this after an edit they don't ask about first (removing, bulk changes): it says what
  // happened and offers Undo right there.
  window.AppHistory = {done: (text) => { say(text, false, true); refresh(); }};

  undoBtn.addEventListener("click", () => step("undo"));
  redoBtn.addEventListener("click", () => step("redo"));
  menu.addEventListener("toggle", () => { if (menu.open) refresh(); });
  document.addEventListener("click", (e) => { if (menu.open && !menu.contains(e.target)) menu.open = false; });

  // Keyboard: Ctrl/Cmd+Z and Ctrl/Cmd+Shift+Z (or Ctrl+Y), except while typing or with a dialog open.
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
    const key = e.key.toLowerCase();
    if (key !== "z" && key !== "y") return;
    const t = e.target;
    if (t.closest("input, textarea, select, [contenteditable], .tabulator-editing") || document.querySelector("dialog[open]")) return;
    e.preventDefault();
    step(key === "y" || e.shiftKey ? "redo" : "undo");
  });

  // Any edit made through the API changes what can be undone, so refresh after it.
  let timer = null;
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const resp = await realFetch(input, init);
    const method = ((init && init.method) || "GET").toUpperCase();
    const url = typeof input === "string" ? input : input.url;
    if (method !== "GET" && url.startsWith(`/api/p/${slug}/`) && !url.startsWith(api("/"))) {
      // Another edit: a toast's Undo would now undo that one instead, so take the offer down.
      if (toast.querySelector(".toast-undo")) toast.hidden = true;
      clearTimeout(timer);
      timer = setTimeout(refresh, 250);
    }
    return resp;
  };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  refresh();
})();
