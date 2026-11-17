(function () {
  "use strict";
  const slug = encodeURIComponent(document.body.dataset.slug);
  const csrf = document.body.dataset.csrf;
  const api = (path) => `/api/p/${slug}${path}`;
  const $ = (id) => document.getElementById(id);
  const status = $("nav-status");
  let sortables = [];

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
    });
    children.forEach((c) => node.append(c));
    return node;
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

  // [text, title, handler] for every action an entry offers.
  function actionsFor(item, siblings, index, menu, parentId, words) {
    const out = [];
    if (index > 0) out.push([words.up, `Move ${item.display} ${words.upTitle || "up"}`,
      () => act(send("POST", "/nav/move", {id: item.id, menu, parent_id: parentId, index: index - 1}), "Moved.")]);
    if (index < siblings.length - 1) out.push([words.down, `Move ${item.display} ${words.downTitle || "down"}`,
      () => act(send("POST", "/nav/move", {id: item.id, menu, parent_id: parentId, index: index + 1}), "Moved.")]);
    out.push(["Move to…", `Move ${item.display} to another menu or under another entry`, () => openMoveTo(item, menu)]);
    out.push(["Add pages…", `Add pages under ${item.display}`, () => openAdd(menu, item.id, item.display)]);
    if (!item.heading) {
      out.push(["Pull in sub-pages", `Add ${item.display}'s sub-pages from the sitemap that aren't under it yet (including ones removed earlier)`,
        () => {
          closed.delete(item.id);  // show what gets pulled in
          rememberClosed();
          act(send("POST", `/nav/${item.id}/subpages`, {depth: 1}), "").then((r) => {
            if (r.ok) status.textContent = r.data.added ? `Added ${r.data.added} sub-page${r.data.added === 1 ? "" : "s"}.`
              : "No sub-pages to add: they're all there already.";
          });
        }]);
    }
    if (menu === "main") out.push(["Add heading", `Add a heading under ${item.display}`, () => addHeading(menu, item.id)]);
    out.push(["Rename", `Rename ${item.display} (or double-click its name)`, () => InlineEdit.startFor(item.id)]);
    out.push(["Remove", `Remove ${item.display} from this menu (the page itself isn't affected)`, async () => {
      const r = await act(send("DELETE", `/nav/${item.id}`), "Removed from the menu.");
      if (r.ok && window.AppHistory) AppHistory.done(`Removed “${item.display}” from the ${MENU_NAMES[menu]}.`);
    }]);
    return out;
  }

  // Top-level main menu pages can show as a button: their chip switches between "link" and "button".
  const buttonable = (item, menu, parentId) => menu === "main" && parentId === null && !item.heading;

  function itemTags(item, canButton) {
    const out = [];
    if (item.heading) out.push(el("span", {class: "tag", text: "heading"}));
    if (canButton) {
      const chip = el("button", {type: "button", class: `tag tag-toggle${item.is_button ? " on" : ""}`,
        "aria-pressed": String(item.is_button), text: item.is_button ? "button" : "link",
        title: item.is_button ? "Shows as a button in the main menu. Click to make it a plain link."
          : "Shows as a plain link. Click to make it a button."});
      chip.addEventListener("click", (e) => {
        e.stopPropagation();
        act(send("PATCH", `/nav/${item.id}`, {is_button: !item.is_button}),
          item.is_button ? `“${item.display}” is a plain link again.` : `“${item.display}” is now a button.`);
      });
      out.push(chip);
    } else if (item.is_button) {
      out.push(el("span", {class: "tag", text: "button"}));
    }
    if (item.is_new) out.push(el("span", {class: "tag", text: "new page"}));
    return out;
  }

  // Double-click a name to rename the entry; clearing a page entry's name goes back to the page's own.
  function renameOpts(item) {
    return {
      value: item.display, allowEmpty: !item.heading,
      hint: item.heading ? "Double-click to rename" : "Double-click to rename. Clear it to use the page's own name.",
      save: (text) => act(send("PATCH", `/nav/${item.id}`, {label: text}),
        text ? "Renamed." : `Back to the page's own name, “${item.page_label}”.`),
    };
  }

  const entriesText = (n) => `${n.toLocaleString()} entr${n === 1 ? "y" : "ies"}`;
  const below = (item) => item.children.reduce((n, c) => n + 1 + below(c), 0);

  // ---- list view ---------------------------------------------------------------

  // Collapsed entries are remembered per browser. Everything starts open; menus are small.
  const CLOSED_KEY = `nav-closed-${document.body.dataset.slug}`;
  let closed = new Set();
  try { closed = new Set(JSON.parse(localStorage.getItem(CLOSED_KEY) || "[]")); } catch (e) { /* private mode */ }
  const rememberClosed = () => {
    try { localStorage.setItem(CLOSED_KEY, JSON.stringify([...closed])); } catch (e) { /* ignore */ }
  };

  // Open or close an entry in place (no redraw, so a drag in progress isn't disturbed).
  function setOpen(li, isOpen) {
    const id = Number(li.dataset.id);
    if (isOpen) closed.delete(id); else closed.add(id);
    rememberClosed();
    li.classList.toggle("collapsed", !isOpen);
    const sub = li.querySelector(":scope > .nav-list");
    if (sub) sub.hidden = !isOpen;
    const toggle = li.querySelector(":scope > .tree-row > .tree-toggle");
    if (toggle) {
      toggle.textContent = isOpen ? "▾" : "▸";
      toggle.setAttribute("aria-expanded", String(isOpen));
      toggle.setAttribute("aria-label", `${isOpen ? "Collapse" : "Expand"} ${toggle.dataset.name}`);
    }
  }

  function row(item, siblings, index, menu, parentId, level) {
    const li = el("li", {"data-id": item.id});
    const name = el("span", {text: item.display});
    InlineEdit.attach(name, renameOpts(item));
    const label = el("span", {class: "tree-label"}, name, ...itemTags(item, buttonable(item, menu, parentId)));
    const info = [];
    if (item.label && item.page_label) info.push(`page: ${item.page_label}`);
    if (item.url_path) info.push(item.url_path);
    const n = below(item);
    if (n) info.push(`${entriesText(n)} below`);
    const hasKids = item.children.length > 0;
    const isOpen = !closed.has(item.id);
    // The toggle sits after the drag handle and isn't part of it, so clicking it never starts a drag.
    const toggle = hasKids
      ? el("button", {type: "button", class: "tree-toggle", "data-name": item.display, "aria-expanded": String(isOpen),
          "aria-label": `${isOpen ? "Collapse" : "Expand"} ${item.display}`, text: isOpen ? "▾" : "▸"})
      : el("span", {class: "tree-toggle tree-toggle-leaf", "aria-hidden": "true"});
    if (hasKids) toggle.addEventListener("click", () => setOpen(li, li.classList.contains("collapsed")));

    const actions = el("span", {class: "tree-actions"});
    actionsFor(item, siblings, index, menu, parentId, {up: "↑", down: "↓"}).forEach(([text, title, fn]) => {
      const b = el("button", {type: "button", class: "link", title, text});
      b.addEventListener("click", fn);
      actions.append(b);
    });

    const parts = [el("span", {class: "tree-handle", title: "Drag to move", "aria-hidden": "true", text: "⠿"}), toggle];
    if (item.problem) {
      parts.push(el("span", {class: "problem-flag", title: item.problem, "aria-label": `Problem: ${item.problem}`, text: "!"}));
    }
    parts.push(label, el("span", {class: "muted small", text: info.join(" · ")}), actions);
    li.append(el("div", {class: "tree-row"}, ...parts));
    // A collapsed entry's sub-list stays in the page (hidden), so dragging the entry still carries it.
    const sub = list(item.children, menu, item.id, level + 1);
    li.append(sub);
    if (hasKids && !isOpen) { li.classList.add("collapsed"); sub.hidden = true; }
    return li;
  }

  function list(items, menu, parentId, level) {
    const ul = el("ul", {class: "tree-list nav-list", "data-menu": menu, "data-parent": parentId === null ? "" : String(parentId)});
    items.forEach((item, i) => ul.append(row(item, items, i, menu, parentId, level)));
    sortables.push(Sortable.create(ul, {
      group: "nav", handle: ".tree-handle", animation: 120, fallbackOnBody: true, swapThreshold: 0.6,
      emptyInsertThreshold: 8, draggable: "li[data-id]",
      onStart: () => { navDragging = true; },
      onEnd: (evt) => {
        navDragging = false;
        clearTimeout(hoverTimer);
        if (evt.from === evt.to && evt.oldDraggableIndex === evt.newDraggableIndex) return;
        const parent = evt.to.dataset.parent;
        act(send("POST", "/nav/move", {id: Number(evt.item.dataset.id), menu: evt.to.dataset.menu,
          parent_id: parent ? Number(parent) : null, index: evt.newDraggableIndex}), "Moved.");
      },
    }));
    return ul;
  }

  // While dragging, holding over a collapsed entry for a moment opens it, so things can be dropped inside.
  let navDragging = false;
  let hoverTimer = null;
  let hoverLi = null;
  document.addEventListener("dragover", (e) => {
    if (!navDragging) return;
    const li = e.target.closest && e.target.closest("#nav-lists li.collapsed");
    if (li === hoverLi) return;
    hoverLi = li;
    clearTimeout(hoverTimer);
    if (li) hoverTimer = setTimeout(() => { if (hoverLi === li) setOpen(li, true); }, 600);
  });

  function setAll(isOpen) {
    document.querySelectorAll("#nav-lists li[data-id]").forEach((li) => {
      if (li.querySelector(":scope > .tree-row > button.tree-toggle")) setOpen(li, isOpen);
    });
  }
  $("collapse-all").addEventListener("click", () => setAll(false));
  $("expand-all").addEventListener("click", () => setAll(true));

  const VIEW_KEY = `nav-view-${document.body.dataset.slug}`;
  let view = new URLSearchParams(window.location.search).get("view");
  try { view = view || localStorage.getItem(VIEW_KEY); } catch (e) { /* private mode */ }
  view = view === "map" ? "map" : "list";
  let menus = null;

  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
    view = b.dataset.view;
    try { localStorage.setItem(VIEW_KEY, view); } catch (e) { /* ignore */ }
    render();
  }));

  async function load() {
    menus = (await (await fetch(api("/nav"))).json()).menus;
    render();
  }

  function render() {
    sortables.forEach((s) => s.destroy());
    sortables = [];
    $("nav-lists").hidden = view !== "list";
    $("nav-map").hidden = view !== "map";
    $("collapse-all").hidden = view !== "list";
    $("expand-all").hidden = view !== "list";
    document.querySelector("main").classList.toggle("full", view === "map");
    document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
    if (view === "map") renderMap(); else renderLists();
  }

  function renderLists() {
    const data = {menus};
    document.querySelectorAll(".menu-panel").forEach((panel) => {
      const menu = panel.dataset.menu;
      const fresh = list(data.menus[menu], menu, null, 1);
      if (!data.menus[menu].length) fresh.append(el("li", {class: "tree-empty muted small", text: "Empty. Add pages or a heading."}));
      panel.querySelector(":scope > .nav-list").replaceWith(fresh);
    });
  }

  // ---- map view: each menu as columns (the top three levels), like a mega menu ----

  // Laid out the way a page shows them: utility nav at the top, then the main menu, then the footer.
  const MENU_TITLES = {utility: "Utility nav", main: "Main menu", footer: "Footer"};

  function entryCard(item, level, menu) {
    const n = below(item);
    const meta = [];
    if (n) meta.push(el("span", {class: "map-count", title: "Entries below this one in the menu", text: entriesText(n)}));
    else if (level === 1) meta.push(el("span", {class: "map-count muted", text: "Nothing below"}));
    if (item.label && item.page_label) meta.push(el("span", {class: "muted small", text: `${n ? " · " : ""}page: ${item.page_label}`}));
    const flag = item.problem
      ? el("span", {class: "problem-flag", title: item.problem, "aria-label": `Problem: ${item.problem}`, text: "!"}) : null;
    return {label: item.display, meta, tags: itemTags(item, level === 1 && buttonable(item, menu, null)), flag, rename: renameOpts(item)};
  }

  function renderMap() {
    const boards = Object.keys(MENU_TITLES).map((menu) => {
      const items = menus[menu];
      const addPages = el("button", {type: "button", text: "Add pages…"});
      addPages.addEventListener("click", () => openAdd(menu, null, null));
      const addHead = el("button", {type: "button", text: menu === "footer" ? "Add column heading" : "Add heading"});
      addHead.addEventListener("click", () => addHeading(menu, null));
      const total = items.reduce((n, i) => n + 1 + below(i), 0);
      const head = el("div", {class: "menu-map-head"},
        el("h2", {text: MENU_TITLES[menu]}),
        el("span", {class: "muted small", text: total ? `${entriesText(total)} · ${items.length} at the top` : "Empty"}),
        el("span", {class: "toolbar"}, addPages, addHead));
      const {row, drop} = MapBoard.board({
        roots: items, card: (item, level) => entryCard(item, level, menu), group: "navmap", root: $("nav-map"), data: {menu},
        emptyText: "Drop entries here", emptyBoard: "Empty. Add pages or a heading, or drag entries here from another menu.", sortables,
        actions: (item, siblings, index, horizontal) => {
          const parentId = findParent(item.id);
          return actionsFor(item, siblings, index, menu, parentId, horizontal
            ? {up: "Move left", down: "Move right", upTitle: "left", downTitle: "right"} : {up: "Move up", down: "Move down"});
        },
        onMove: ({id, parentId, index, to}) => act(send("POST", "/nav/move", {id, menu: to.menu, parent_id: parentId, index}), "Moved."),
      });
      return el("section", {class: "menu-map", "data-menu": menu}, head, drop, items.length ? el("div", {class: "map-board"}, row) : "");
    });
    $("nav-map").replaceChildren(...boards);
  }

  function findParent(id) {
    const search = (items, parent) => {
      for (const i of items) {
        if (i.id === id) return {found: true, parent};
        const r = search(i.children, i.id);
        if (r.found) return r;
      }
      return {found: false};
    };
    for (const m of Object.keys(menus)) {
      const r = search(menus[m], null);
      if (r.found) return r.parent;
    }
    return null;
  }

  // ---- move to: any menu, any spot, without dragging --------------------------

  const MAX_LEVELS = {main: 4, utility: 2, footer: 2};
  let movingEntry = null;

  function openMoveTo(item, fromMenu) {
    movingEntry = item;
    const height = (i) => 1 + Math.max(0, ...i.children.map(height));
    const h = height(item);
    const select = $("me-dest");
    select.replaceChildren();
    ["main", "utility", "footer"].forEach((menu) => {
      const group = el("optgroup", {label: MENU_TITLES[menu]});
      group.append(el("option", {value: `${menu}:`, text: `${MENU_TITLES[menu]}: top level`}));
      const walk = (items, trail, level) => items.forEach((i) => {
        if (i.id === item.id) return;  // not into itself or anything below it
        const path = trail.concat(i.display);
        if (level + h <= MAX_LEVELS[menu]) group.append(el("option", {value: `${menu}:${i.id}`, text: `${"\u00a0\u00a0".repeat(level)}${path.join(" › ")}`}));
        walk(i.children, path, level + 1);
      });
      walk(menus[menu], [], 1);
      select.append(group);
    });
    const parent = findParent(item.id);
    select.value = `${fromMenu}:${parent === null ? "" : parent}`;
    $("me-title").textContent = `Move “${item.display}”`;
    $("me-hint").textContent = h > 1 ? `Everything under it (${entriesText(below(item))}) moves with it. It goes at the end of the spot you pick.`
      : "It goes at the end of the spot you pick; drag it afterwards to fine-tune the order.";
    $("me-error").textContent = "";
    $("move-entry-dialog").showModal();
    select.focus();
  }

  $("me-cancel").addEventListener("click", () => $("move-entry-dialog").close());
  $("me-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const [menu, parent] = $("me-dest").value.split(":");
    if (parent) { closed.delete(Number(parent)); rememberClosed(); }
    const r = await send("POST", "/nav/move", {id: movingEntry.id, menu, parent_id: parent ? Number(parent) : null, index: 100000});
    if (!r.ok) { $("me-error").textContent = r.data.detail || "That didn't work."; return; }
    $("move-entry-dialog").close();
    status.textContent = `Moved “${movingEntry.display}”.`;
    await load();
  });

  // ---- actions -----------------------------------------------------------------

  function addHeading(menu, parentId) {
    const label = window.prompt("Heading name:");
    if (!label || !label.trim()) return;
    act(send("POST", "/nav/headings", {menu, parent_id: parentId, label}), `Added the heading “${label.trim()}”.`);
  }

  document.querySelectorAll("[data-add-heading]").forEach((b) =>
    b.addEventListener("click", () => addHeading(b.dataset.addHeading, null)));
  document.querySelectorAll("[data-add-pages]").forEach((b) =>
    b.addEventListener("click", () => openAdd(b.dataset.addPages, null, null)));

  // ---- add-pages dialog --------------------------------------------------------

  const dialog = $("add-dialog");
  let adding = null;  // {menu, parentId, chosen: Map(id -> label)}
  const MENU_NAMES = {main: "main menu", utility: "utility nav", footer: "footer"};

  function updateCount() {
    const n = adding.chosen.size;
    $("add-count").textContent = n ? `${n} chosen: ${[...adding.chosen.values()].join(", ")}` : "Nothing chosen yet.";
    $("add-save").textContent = n ? `Add ${n} page${n === 1 ? "" : "s"}` : "Add";
  }

  function openAdd(menu, parentId, parentLabel) {
    adding = {menu, parentId, chosen: new Map()};
    $("add-title").textContent = parentLabel ? `Add pages under “${parentLabel}”` : `Add pages to the ${MENU_NAMES[menu]}`;
    $("add-search").value = "";
    $("add-results").replaceChildren();
    $("add-error").textContent = "";
    $("add-depth").value = "0";
    updateCount();
    dialog.showModal();
    $("add-search").focus();
  }

  let timer = null;
  $("add-search").addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = $("add-search").value.trim();
      if (!q) { $("add-results").replaceChildren(); return; }
      const pages = await (await fetch(api(`/targets?q=${encodeURIComponent(q)}`))).json();
      $("add-results").replaceChildren(...(pages.length ? pages.map((p) => {
        const b = el("button", {type: "button", "aria-pressed": String(adding.chosen.has(p.id))},
          el("span", {text: p.label}), el("span", {class: "muted", text: ` ${p.url_path || "(new page)"}`}));
        b.addEventListener("click", () => {
          if (adding.chosen.has(p.id)) adding.chosen.delete(p.id); else adding.chosen.set(p.id, p.label);
          b.setAttribute("aria-pressed", String(adding.chosen.has(p.id)));
          updateCount();
        });
        return el("li", {}, b);
      }) : [el("li", {class: "muted", text: "No matching pages."})]));
    }, 200);
  });

  $("add-cancel").addEventListener("click", () => dialog.close());
  $("add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!adding.chosen.size) { $("add-error").textContent = "Click pages in the results to choose them."; return; }
    if (adding.parentId !== null) { closed.delete(adding.parentId); rememberClosed(); }  // show what was added
    const r = await send("POST", "/nav/pages", {menu: adding.menu, parent_id: adding.parentId,
      page_ids: [...adding.chosen.keys()], subpage_depth: Number($("add-depth").value)});
    if (!r.ok) { $("add-error").textContent = r.data.detail || "That didn't work."; return; }
    dialog.close();
    status.textContent = r.data.added ? `Added ${r.data.added} entr${r.data.added === 1 ? "y" : "ies"}.` : "Those pages were already there.";
    await load();
  });

  load();
  // Undo or redo in the top bar: reload this tab's data in place instead of the whole page.
  document.addEventListener("history:changed", (e) => { e.preventDefault(); load(); });
})();
