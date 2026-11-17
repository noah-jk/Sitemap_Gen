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

  // ---- shared by both views ----------------------------------------------------

  const MENU_TAGS = {main: "main menu", utility: "utility", footer: "footer"};
  const TYPES = {landing: "Landing", listing: "Listing", list: "List", article: "Article", form: "Form"};

  function tags(node) {
    const out = [];
    if (node.is_new) out.push(el("span", {class: "tag", text: "new"}));
    node.menus.forEach((m) => out.push(el("span", {class: "tag menu-tag", text: MENU_TAGS[m]})));
    const type = el("button", {type: "button", class: "tag tag-edit",
      title: node.page_type ? "Page type. Click to change it." : "Page type, worked out from the sitemap. Click to set it.",
      text: TYPES[node.type] + (node.page_type ? "" : " (auto)")});
    type.addEventListener("click", (e) => { e.stopPropagation(); editType(node, type); });
    out.push(type);
    return out;
  }

  // The type tag turns into a dropdown in place; picking a type saves it, Esc or clicking away keeps it.
  function editType(node, tag) {
    const select = el("select", {class: "tag-select", "aria-label": `Page type for ${node.label}`});
    select.append(el("option", {value: "", text: node.page_type ? "Auto" : `Auto (${TYPES[node.type]})`}));
    Object.entries(TYPES).forEach(([k, v]) => select.append(el("option", {value: k, text: v})));
    select.value = node.page_type || "";
    tag.replaceWith(select);
    select.focus();
    try { select.showPicker(); } catch (e) { /* not every browser can open it for us */ }
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      if (save && select.value !== (node.page_type || "")) {
        patch(node, {page_type: select.value || null}, `Page type set to ${select.value ? TYPES[select.value] : "Auto"}.`);
      } else {
        select.replaceWith(tag);
      }
    };
    select.addEventListener("change", () => finish(true));
    select.addEventListener("blur", () => finish(false));
    select.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); finish(false); } });
    ["mousedown", "click"].forEach((t) => select.addEventListener(t, (e) => e.stopPropagation()));
  }

  // Double-click a page's name to rename it.
  const renameOpts = (node) => ({value: node.label, save: (text) => patch(node, {label: text}, "Renamed.")});

  function pagesText(n) {
    return `${n.toLocaleString()} page${n === 1 ? "" : "s"}`;
  }

  // [text, title, handler] for every action a page offers, in both views.
  function actionsFor(node, siblings, index, words) {
    const list = [];
    if (index > 0) list.push([words.up, `Move ${node.label} ${words.upTitle || "up"}`,
      () => act(send("POST", "/tree/move", {id: node.id, parent_id: node.parentId, index: index - 1}), "Moved.")]);
    if (index < siblings.length - 1) list.push([words.down, `Move ${node.label} ${words.downTitle || "down"}`,
      () => act(send("POST", "/tree/move", {id: node.id, parent_id: node.parentId, index: index + 1}), "Moved.")]);
    list.push(["Move…", `Move ${node.label} under another page`, () => openMove(node)]);
    list.push(["Add sub-page", `Add a page under ${node.label}`, () => addPage(node.id)]);
    list.push(["Rename", `Rename ${node.label} (or double-click its name)`, () => InlineEdit.startFor(node.id)]);
    list.push(["Add to menu…", `Add ${node.label} to a menu`, () => MenuPicker.open({
      api, csrf, title: `Add “${node.label}” to a menu`, target: {page_ids: [node.id]},
      onDone: (added, menuName) => {
        status.textContent = added ? `Added to the ${menuName}.` : `Already in that spot in the ${menuName}.`;
        load();
      }})]);
    if (node.is_new) list.push(["Remove", `Remove ${node.label}`, () => removePage(node)]);
    return list;
  }

  // ---- list view ---------------------------------------------------------------

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
    const name = el("span", {text: node.label});
    InlineEdit.attach(name, renameOpts(node));
    const label = el("span", {class: "tree-label"}, name, ...tags(node));

    const actions = el("span", {class: "tree-actions"});
    actionsFor(node, siblings, index, {up: "↑", down: "↓"}).forEach(([text, title, fn]) => {
      const b = el("button", {type: "button", class: "link", title, text});
      b.addEventListener("click", fn);
      actions.append(b);
    });

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
  const VIEW_KEY = `tree-view-${slug}`;
  let view = new URLSearchParams(window.location.search).get("view");
  try { view = view || localStorage.getItem(VIEW_KEY); } catch (e) { /* private mode */ }
  view = view === "map" ? "map" : "list";

  function render() {
    sortables.forEach((s) => s.destroy());
    sortables = [];
    const total = roots.reduce((n, r) => n + 1 + r.descendants, 0);
    $("list-view").hidden = view !== "list";
    $("map").hidden = view !== "map";
    $("collapse-all").hidden = view !== "list";
    document.querySelector("main").classList.toggle("full", view === "map");
    document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
    if (view === "map") {
      renderMap(total);
      return;
    }
    const top = list(roots, null);
    top.id = "tree";
    $("tree").replaceWith(top);
    $("tree-total").textContent = pagesText(total);
  }

  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
    view = b.dataset.view;
    try { localStorage.setItem(VIEW_KEY, view); } catch (e) { /* ignore */ }
    render();
  }));

  // ---- map view: the top three levels as an org chart, with counts for what's below ----

  function mapCard(node, level) {
    const below = node.descendants
      ? el("span", {class: "map-count", title: "Pages below this one", text: pagesText(node.descendants)})
      : el("span", {class: "map-count muted", text: level === 1 ? "No sub-pages" : ""});
    const meta = [below];
    if (node.absorbs) meta.push(el("span", {class: "muted small", text: ` · absorbs ${node.absorbs}`}));
    return {label: node.label, meta, tags: tags(node), flag: null, rename: renameOpts(node)};
  }

  function renderMap(total) {
    const {row, drop} = MapBoard.board({
      roots, card: mapCard, group: "map", root: $("map"), emptyText: "Drop pages here", emptyBoard: "No pages yet.", sortables,
      actions: (node, siblings, index, horizontal) => actionsFor(node, siblings, index, horizontal
        ? {up: "Move left", down: "Move right", upTitle: "left", downTitle: "right"} : {up: "Move up", down: "Move down"}),
      onMove: ({id, parentId, index}) => act(send("POST", "/tree/move", {id, parent_id: parentId, index}), "Moved."),
    });
    const addTop = el("button", {type: "button", class: "map-add", text: "+ Add top-level page"});
    addTop.addEventListener("click", () => addPage(null));
    const home = el("div", {class: "map-home"}, el("strong", {text: "Home"}),
      el("span", {class: "muted small", text: ` ${pagesText(total)} · ${roots.length} top-level`}));
    $("map").replaceChildren(home, drop, el("div", {class: "map-board"}, row, addTop));
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

  async function addPage(parentId) {
    const label = window.prompt("Name of the new page:");
    if (!label || !label.trim()) return;
    if (parentId !== null) { open.add(parentId); remember(); }
    await act(send("POST", "/tree/pages", {label, parent_id: parentId}), `Added “${label.trim()}”.`);
  }

  async function removePage(node) {
    const kids = node.children.length ? ` Its ${pagesText(node.children.length)} moved up a level.` : "";
    const r = await act(send("DELETE", `/tree/pages/${node.id}`), "Removed.");
    if (r.ok && window.AppHistory) AppHistory.done(`Removed the new page “${node.label}”.${kids}`);
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
  // Undo or redo in the top bar: reload this tab's data in place instead of the whole page.
  document.addEventListener("history:changed", (e) => { e.preventDefault(); load(); });
})();
