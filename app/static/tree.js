(function () {
  "use strict";
  const slug = encodeURIComponent(document.body.dataset.slug);
  const csrf = document.body.dataset.csrf;
  const api = (path) => `/api/p/${slug}${path}`;
  const $ = (id) => document.getElementById(id);
  const status = $("tree-status");
  const STORE_KEY = `tree-open-${slug}`;
  let open = new Set();
  try { open = new Set(JSON.parse(localStorage.getItem(STORE_KEY) || "[]")); } catch (e) { /* private mode */ }
  let byId = new Map();
  let sortables = [];

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
    });
    children.forEach((c) => node.append(c));
    return node;
  }

  function remember() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify([...open])); } catch (e) { /* ignore */ }
  }

  async function send(method, path, payload) {
    let resp;
    try {
      resp = await fetch(api(path), {
        method, headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: payload === undefined ? undefined : JSON.stringify(payload),
      });
    } catch (e) {
      return {ok: false, data: {detail: "Couldn't reach the server. Nothing was changed."}};
    }
    if (resp.status === 401) window.location.href = "/login";
    return {ok: resp.ok, status: resp.status, data: await resp.json().catch(() => ({}))};
  }

  async function act(promise, done) {
    const r = await promise;
    status.textContent = r.ok ? done : (r.data.detail || "That didn't work.");
    await load();
    return r;
  }

  // ---- rendering ---------------------------------------------------------------

  function nodeRow(node, siblings, index) {
    const li = el("li", {"data-id": node.id});
    const hasKids = node.children.length > 0;
    const isOpen = open.has(node.id);
    const toggle = el("button", {type: "button", class: "tree-toggle", "aria-expanded": String(isOpen),
      "aria-label": `${isOpen ? "Close" : "Open"} ${node.label}`, text: isOpen ? "▾" : (hasKids ? "▸" : "▹")});
    toggle.addEventListener("click", () => {
      if (open.has(node.id)) open.delete(node.id); else open.add(node.id);
      remember();
      render();
    });

    const meta = [];
    if (node.descendants) meta.push(`${node.descendants.toLocaleString()} below`);
    if (node.collection_count) meta.push(`incl. ${node.collection_count.toLocaleString()} collection items`);
    if (node.absorbs) meta.push(`absorbs ${node.absorbs}`);
    const label = el("span", {class: "tree-label"}, el("span", {text: node.label}));
    if (node.is_new) label.append(el("span", {class: "tag", text: "new"}));
    if (node.is_button) label.append(el("span", {class: "tag", text: "button"}));

    const actions = el("span", {class: "tree-actions"});
    const add = (text, title, fn) => {
      const b = el("button", {type: "button", class: "link", title, text});
      b.addEventListener("click", fn);
      actions.append(b);
    };
    add("↑", `Move ${node.label} up`, () => index > 0 &&
      act(send("POST", "/tree/move", {id: node.id, parent_id: node.parentId, index: index - 1}), "Moved up."));
    add("↓", `Move ${node.label} down`, () => index < siblings.length - 1 &&
      act(send("POST", "/tree/move", {id: node.id, parent_id: node.parentId, index: index + 1}), "Moved down."));
    add("Move…", `Move ${node.label} under another page`, () => openMove(node));
    add("Add sub-page", `Add a page under ${node.label}`, () => addPage(node.id));
    add("Rename", `Rename ${node.label}`, () => rename(node));
    if (node.parentId === null) {
      add(node.is_button ? "Unmake button" : "Make button", "Show as a button in the main navigation",
        () => patch(node, {is_button: !node.is_button}, node.is_button ? "No longer a button." : "Now a nav button."));
    }
    if (node.is_new) add("Remove", `Remove ${node.label}`, () => removePage(node));

    const row = el("div", {class: "tree-row"},
      el("span", {class: "tree-handle", title: "Drag to move", "aria-hidden": "true", text: "⠿"}),
      toggle, label,
      el("span", {class: "muted small", text: [node.url_path || "", ...meta].filter(Boolean).join(" · ")}),
      actions);
    li.append(row);
    if (isOpen) li.append(list(node.children, node.id));
    return li;
  }

  function list(nodes, parentId) {
    const ul = el("ul", {class: "tree-list", "data-parent": parentId === null ? "" : String(parentId)});
    if (!nodes.length) ul.append(el("li", {class: "tree-empty muted small", text: "No sub-pages. Drop pages here."}));
    nodes.forEach((n, i) => ul.append(nodeRow(n, nodes, i)));
    sortables.push(Sortable.create(ul, {
      group: "tree", handle: ".tree-handle", animation: 120, fallbackOnBody: true, swapThreshold: 0.6,
      filter: ".tree-empty", draggable: "li[data-id]",
      onEnd: (evt) => {
        if (evt.from === evt.to && evt.oldDraggableIndex === evt.newDraggableIndex) return;
        const parent = evt.to.dataset.parent;
        act(send("POST", "/tree/move", {id: Number(evt.item.dataset.id), parent_id: parent ? Number(parent) : null,
          index: evt.newDraggableIndex}), "Moved.");
      },
    }));
    return ul;
  }

  let roots = [];
  function render() {
    sortables.forEach((s) => s.destroy());
    sortables = [];
    const top = list(roots, null);
    top.id = "tree";
    $("tree").replaceWith(top);
    const total = roots.reduce((n, r) => n + 1 + r.descendants, 0);
    $("tree-total").textContent = `${total.toLocaleString()} pages`;
  }

  async function load() {
    const resp = await fetch(api("/tree"));
    const data = await resp.json();
    byId = new Map();
    const index = (nodes, parentId) => nodes.forEach((n) => { n.parentId = parentId; byId.set(n.id, n); index(n.children, n.id); });
    index(data.roots, null);
    roots = data.roots;
    render();
  }

  // ---- actions -----------------------------------------------------------------

  async function patch(node, changes, done) {
    let r = await send("PATCH", `/pages/${node.id}`, {changes, updated_at: node.updated_at});
    if (r.status === 409) {  // the page moved since the tree loaded; retry against the fresh copy
      await load();
      const fresh = byId.get(node.id);
      if (fresh) r = await send("PATCH", `/pages/${node.id}`, {changes, updated_at: fresh.updated_at});
    }
    status.textContent = r.ok ? done : (r.data.detail || "That didn't work.");
    await load();
  }

  function rename(node) {
    const label = window.prompt("New name for this page:", node.label);
    if (label && label.trim() && label.trim() !== node.label) patch(node, {label}, "Renamed.");
  }

  async function addPage(parentId) {
    const label = window.prompt("Name of the new page:");
    if (!label || !label.trim()) return;
    if (parentId !== null) { open.add(parentId); remember(); }
    await act(send("POST", "/tree/pages", {label, parent_id: parentId}), `Added “${label.trim()}”.`);
  }

  function removePage(node) {
    const kids = node.children.length ? ` Its ${node.children.length} sub-page(s) move up a level.` : "";
    if (window.confirm(`Remove the new page “${node.label}”?${kids}`)) {
      act(send("DELETE", `/tree/pages/${node.id}`), "Removed.");
    }
  }

  $("add-top").addEventListener("click", () => addPage(null));
  $("collapse-all").addEventListener("click", () => { open.clear(); remember(); render(); });

  // ---- move dialog -------------------------------------------------------------

  const dialog = $("move-dialog");
  let moving = null;

  function openMove(node) {
    moving = node;
    $("mv-title").textContent = `Move “${node.label}”`;
    $("mv-search").value = "";
    $("mv-results").replaceChildren();
    $("mv-error").textContent = "";
    dialog.showModal();
    $("mv-search").focus();
  }

  async function moveTo(parentId) {
    const r = await send("POST", "/tree/move", {id: moving.id, parent_id: parentId, index: 100000});
    if (!r.ok) { $("mv-error").textContent = r.data.detail || "That didn't work."; return; }
    if (parentId !== null) { open.add(parentId); remember(); }
    dialog.close();
    status.textContent = "Moved.";
    await load();
  }

  $("mv-top").addEventListener("click", () => moveTo(null));
  $("mv-cancel").addEventListener("click", () => dialog.close());
  let timer = null;
  $("mv-search").addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = $("mv-search").value.trim();
      if (!q) { $("mv-results").replaceChildren(); return; }
      const resp = await fetch(api(`/targets?q=${encodeURIComponent(q)}&exclude=${moving.id}`));
      const pages = (await resp.json()).filter((p) => byId.has(p.id));
      $("mv-results").replaceChildren(...(pages.length ? pages.map((p) => {
        const b = el("button", {type: "button"}, el("span", {text: p.label}),
          el("span", {class: "muted", text: ` ${p.url_path || "(new page)"}`}));
        b.addEventListener("click", () => moveTo(p.id));
        return el("li", {}, b);
      }) : [el("li", {class: "muted", text: "No matching pages in the tree."})]));
    }, 200);
  });

  load();
})();
