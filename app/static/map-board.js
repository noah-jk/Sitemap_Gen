// The column map shared by the Sitemap and Navigation tabs: each top-level item is a column, its
// children are cards below it, and their children are smaller cards inside those. Nothing deeper is
// drawn; cards say what's below them instead. Drag the ⠿ handle to move columns and cards.
window.MapBoard = (function () {
  "use strict";

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
    });
    children.forEach((c) => node.append(c));
    return node;
  }

  // Close an open card menu when clicking anywhere else.
  document.addEventListener("click", (e) => {
    document.querySelectorAll(".map-menu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });

  function cardMenu(label, actions) {
    const items = el("div", {class: "map-menu-items"});
    const menu = el("details", {class: "map-menu"},
      el("summary", {"aria-label": `Actions for ${label}`, title: "Actions", text: "⋯"}), items);
    actions.forEach(([text, title, fn]) => {
      const b = el("button", {type: "button", class: "link", title, text});
      b.addEventListener("click", () => { menu.open = false; fn(); });
      items.append(b);
    });
    return menu;
  }

  /*
   * board(opts) returns {row, drop}: the row of columns, and the strip to put above it where a card
   * or column dropped becomes the first top-level item.
   *   roots      top-level items, each with .id and .children
   *   card(item, level) -> {label, meta: [Node], tags: [Node], flag: Node|null, rename: InlineEdit options}
   *   actions(item, siblings, index, horizontal) -> [[text, title, handler], ...]
   *   onMove({id, parentId, index, to})   to = the dataset of the list it was dropped in
   *   data       extra data-* attributes on every list (the Navigation tab marks the menu)
   *   group      drag group; boards sharing a group can swap cards and columns
   *   emptyBoard text for a board with no columns (it becomes a drop area)
   *   root       element that gets .map-dragging while dragging (drop areas show)
   *   emptyText  shown in a column with no cards
   *   sortables  array the Sortable instances are added to (destroy them before redrawing)
   */
  function board(opts) {
    const dragging = (on) => () => opts.root.classList.toggle("map-dragging", on);
    const data = Object.fromEntries(Object.entries(opts.data || {}).map(([k, v]) => [`data-${k}`, v]));
    // Cards move between lists and columns between rows. A card or column dropped on the strip above
    // the row becomes the first column.
    const onEnd = (evt) => {
      dragging(false)();
      if (evt.from === evt.to && evt.oldDraggableIndex === evt.newDraggableIndex) return;
      const parent = evt.to.dataset.parent;
      const index = evt.to.dataset.top ? 0 : evt.newDraggableIndex;
      opts.onMove({id: Number(evt.item.dataset.id), parentId: parent ? Number(parent) : null, index, to: evt.to.dataset});
    };

    function card(item, siblings, index, level) {
      const c = opts.card(item, level);
      const node = el("div", {class: `map-card map-l${level}`, "data-id": item.id});
      const head = el("div", {class: "map-head"},
        el("span", {class: "map-grip", title: "Drag to move", "aria-hidden": "true", text: "⠿"}));
      if (c.flag) head.append(c.flag);
      const name = el("span", {class: "map-label", text: c.label});
      if (c.rename && window.InlineEdit) InlineEdit.attach(name, c.rename);  // double-click to rename
      head.append(name, cardMenu(c.label, opts.actions(item, siblings, index, level === 1)));
      node.append(head, el("div", {class: "map-meta"}, ...c.meta), el("div", {class: "map-tags"}, ...c.tags));
      if (level === 2) node.append(list(item.children, item.id, 3));
      return node;
    }

    function list(items, parentId, level) {
      const box = el("div", Object.assign({class: `map-list map-list-l${level}`, "data-parent": String(parentId)}, data));
      items.forEach((item, i) => box.append(card(item, items, i, level)));
      if (!items.length && level === 2) box.append(el("div", {class: "map-empty muted small", text: opts.emptyText}));
      opts.sortables.push(Sortable.create(box, {
        group: opts.group, handle: ".map-grip", draggable: ".map-card", filter: ".map-empty, .map-menu",
        preventOnFilter: false, animation: 120, fallbackOnBody: true, swapThreshold: 0.65, emptyInsertThreshold: 12,
        onStart: dragging(true), onEnd,
      }));
      return box;
    }

    const row = el("div", Object.assign({class: "map-row", "data-parent": ""}, data));
    opts.roots.forEach((item, i) => {
      const col = el("section", {class: "map-col", "data-id": item.id, "aria-label": opts.card(item, 1).label});
      col.append(card(item, opts.roots, i, 1), list(item.children, item.id, 2));
      row.append(col);
    });
    opts.sortables.push(Sortable.create(row, {
      group: `${opts.group}-top`, handle: ".map-l1 > .map-head > .map-grip", draggable: ".map-col",
      direction: "horizontal", animation: 120, onStart: dragging(true), onEnd,
    }));
    const empty = !opts.roots.length;
    const drop = el("div", Object.assign({class: `map-topdrop${empty ? " map-topdrop-empty" : ""}`, "data-parent": "",
      "data-top": "1"}, data), el("span", {class: "muted small",
      text: empty ? opts.emptyBoard : "⇣ Drop a card here to make it top-level (first column)"}));
    opts.sortables.push(Sortable.create(drop, {
      group: {name: `${opts.group}-drop`, pull: false, put: [opts.group, `${opts.group}-top`]},
      draggable: ".map-card, .map-col", emptyInsertThreshold: 16, sort: false,
    }));
    return {row, drop};
  }

  return {board, el};
})();
