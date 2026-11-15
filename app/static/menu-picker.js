// "Add to menu…" dialog shared by the Pages grid and the Sitemap tab.
window.MenuPicker = (function () {
  "use strict";
  const NAMES = {main: "Main menu", utility: "Utility nav", footer: "Footer"};
  let dialog, form, title, menuSelect, parentSelect, depthSelect, error, opts;

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
    });
    children.forEach((c) => node.append(c));
    return node;
  }

  function build() {
    title = el("h2", {id: "mp-title"});
    menuSelect = el("select", {id: "mp-menu"}, ...Object.entries(NAMES).map(([k, v]) => el("option", {value: k, text: v})));
    parentSelect = el("select", {id: "mp-parent"});
    depthSelect = el("select", {id: "mp-depth"},
      el("option", {value: "0", text: "No, just the page(s)"}), el("option", {value: "1", text: "Yes, 1 level down"}),
      el("option", {value: "2", text: "Yes, 2 levels down"}), el("option", {value: "3", text: "Yes, 3 levels down"}));
    error = el("p", {class: "error", role: "alert"});
    const cancel = el("button", {type: "button", text: "Cancel"});
    form = el("form", {},
      title,
      el("label", {}, "Menu", menuSelect),
      el("label", {}, "Put it", parentSelect),
      el("label", {}, "Include sub-pages from the sitemap", depthSelect),
      el("p", {class: "muted small", text: "Pages already in that spot are skipped. Rename entries in the Navigation tab."}),
      error,
      el("div", {class: "row end"}, cancel, el("button", {type: "submit", class: "primary", text: "Add"})));
    dialog = el("dialog", {"aria-labelledby": "mp-title"}, form);
    document.body.append(dialog);
    menuSelect.addEventListener("change", loadParents);
    cancel.addEventListener("click", () => dialog.close());
    form.addEventListener("submit", submit);
  }

  async function loadParents() {
    const data = await (await fetch(opts.api("/nav"))).json();
    const options = [el("option", {value: "", text: "At the top level"})];
    const walk = (items, depth) => items.forEach((i) => {
      options.push(el("option", {value: String(i.id), text: `${"— ".repeat(depth)}Under “${i.display}”`}));
      walk(i.children, depth + 1);
    });
    walk(data.menus[menuSelect.value], 0);
    parentSelect.replaceChildren(...options);
  }

  async function submit(e) {
    e.preventDefault();
    const payload = Object.assign({menu: menuSelect.value, parent_id: parentSelect.value ? Number(parentSelect.value) : null,
      subpage_depth: Number(depthSelect.value)}, opts.target);
    let resp;
    try {
      resp = await fetch(opts.api("/nav/pages"), {method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": opts.csrf}, body: JSON.stringify(payload)});
    } catch (err) {
      error.textContent = "Couldn't reach the server. Nothing was added.";
      return;
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { error.textContent = data.detail || "That didn't work."; return; }
    dialog.close();
    opts.onDone(data.added, NAMES[menuSelect.value]);
  }

  return {
    // opts: {api, csrf, title, target: {page_ids} | {match}, onDone(added, menuName)}
    async open(o) {
      if (!dialog) build();
      opts = o;
      title.textContent = o.title;
      error.textContent = "";
      depthSelect.value = "0";
      await loadParents();
      dialog.showModal();
      menuSelect.focus();
    },
  };
})();
