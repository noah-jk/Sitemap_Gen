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

  // ---- rendering ---------------------------------------------------------------

  function row(item, siblings, index, menu, parentId, level) {
    const li = el("li", {"data-id": item.id});
    const label = el("span", {class: "tree-label"}, el("span", {text: item.display}));
    if (item.heading) label.append(el("span", {class: "tag", text: "heading"}));
    if (item.is_button) label.append(el("span", {class: "tag", text: "button"}));
    if (item.is_new) label.append(el("span", {class: "tag", text: "new page"}));
    const info = [];
    if (item.label && item.page_label) info.push(`page: ${item.page_label}`);
    if (item.url_path) info.push(item.url_path);

    const actions = el("span", {class: "tree-actions"});
    const add = (text, title, fn) => {
      const b = el("button", {type: "button", class: "link", title, text});
      b.addEventListener("click", fn);
      actions.append(b);
    };
    add("↑", `Move ${item.display} up`, () => index > 0 &&
      act(send("POST", "/nav/move", {id: item.id, menu, parent_id: parentId, index: index - 1}), "Moved up."));
    add("↓", `Move ${item.display} down`, () => index < siblings.length - 1 &&
      act(send("POST", "/nav/move", {id: item.id, menu, parent_id: parentId, index: index + 1}), "Moved down."));
    add("Add pages…", `Add pages under ${item.display}`, () => openAdd(menu, item.id, item.display));
    if (!item.heading) {
      add("Pull in sub-pages", `Add ${item.display}'s sub-pages from the sitemap that aren't under it yet (including ones removed earlier)`,
        () => act(send("POST", `/nav/${item.id}/subpages`, {depth: 1}), "").then((r) => {
          if (r.ok) status.textContent = r.data.added ? `Added ${r.data.added} sub-page${r.data.added === 1 ? "" : "s"}.`
            : "No sub-pages to add: they're all there already.";
        }));
    }
    if (menu === "main") add("Add heading", `Add a heading under ${item.display}`, () => addHeading(menu, item.id));
    add("Rename", `Rename ${item.display}`, () => rename(item));
    if (menu === "main" && parentId === null) {
      add(item.is_button ? "Unmake button" : "Make button", "Show as a button in the main menu",
        () => act(send("PATCH", `/nav/${item.id}`, {is_button: !item.is_button}),
          item.is_button ? "No longer a button." : "Now a button."));
    }
    add("Remove", `Remove ${item.display} from this menu`, () => {
      if (window.confirm(`Remove “${item.display}” from this menu? The page itself isn't affected.`)) {
        act(send("DELETE", `/nav/${item.id}`), "Removed from the menu.");
      }
    });

    const parts = [el("span", {class: "tree-handle", title: "Drag to move", "aria-hidden": "true", text: "⠿"})];
    if (item.problem) {
      parts.push(el("span", {class: "problem-flag", title: item.problem, "aria-label": `Problem: ${item.problem}`, text: "!"}));
    }
    parts.push(label, el("span", {class: "muted small", text: info.join(" · ")}), actions);
    li.append(el("div", {class: "tree-row"}, ...parts));
    li.append(list(item.children, menu, item.id, level + 1));
    return li;
  }

  function list(items, menu, parentId, level) {
    const ul = el("ul", {class: "tree-list nav-list", "data-menu": menu, "data-parent": parentId === null ? "" : String(parentId)});
    items.forEach((item, i) => ul.append(row(item, items, i, menu, parentId, level)));
    sortables.push(Sortable.create(ul, {
      group: "nav", handle: ".tree-handle", animation: 120, fallbackOnBody: true, swapThreshold: 0.6,
      emptyInsertThreshold: 8, draggable: "li[data-id]",
      onEnd: (evt) => {
        if (evt.from === evt.to && evt.oldDraggableIndex === evt.newDraggableIndex) return;
        const parent = evt.to.dataset.parent;
        act(send("POST", "/nav/move", {id: Number(evt.item.dataset.id), menu: evt.to.dataset.menu,
          parent_id: parent ? Number(parent) : null, index: evt.newDraggableIndex}), "Moved.");
      },
    }));
    return ul;
  }

  async function load() {
    const data = await (await fetch(api("/nav"))).json();
    sortables.forEach((s) => s.destroy());
    sortables = [];
    document.querySelectorAll(".menu-panel").forEach((panel) => {
      const menu = panel.dataset.menu;
      const fresh = list(data.menus[menu], menu, null, 1);
      if (!data.menus[menu].length) fresh.append(el("li", {class: "tree-empty muted small", text: "Empty. Add pages or a heading."}));
      panel.querySelector(":scope > .nav-list").replaceWith(fresh);
    });
  }

  // ---- actions -----------------------------------------------------------------

  function rename(item) {
    const hint = item.heading ? "Heading name:" : "Menu label (leave empty to use the page's own name):";
    const label = window.prompt(hint, item.label || item.display);
    if (label === null) return;
    act(send("PATCH", `/nav/${item.id}`, {label}), "Renamed.");
  }

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
    const r = await send("POST", "/nav/pages", {menu: adding.menu, parent_id: adding.parentId,
      page_ids: [...adding.chosen.keys()], subpage_depth: Number($("add-depth").value)});
    if (!r.ok) { $("add-error").textContent = r.data.detail || "That didn't work."; return; }
    dialog.close();
    status.textContent = r.data.added ? `Added ${r.data.added} entr${r.data.added === 1 ? "y" : "ies"}.` : "Those pages were already there.";
    await load();
  });

  load();
})();
