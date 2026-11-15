(function () {
  "use strict";
  const body = document.body;
  const slug = encodeURIComponent(body.dataset.slug);
  const csrf = body.dataset.csrf;
  const DISPOSITIONS = body.dataset.dispositions.split(",");
  const NEEDS_TARGET = ["combine", "split"];
  const api = (path) => `/api/p/${slug}${path}`;
  const $ = (id) => document.getElementById(id);
  const status = $("grid-status");

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
      return {ok: false, status: 0, data: {detail: "Couldn't reach the server. Nothing was saved."}};
    }
    if (resp.status === 401) { window.location.href = "/login"; }
    const data = await resp.json().catch(() => ({}));
    return {ok: resp.ok, status: resp.status, data};
  }

  const effective = (row) => row.decision || row.recommendation || "";

  // ---- views -----------------------------------------------------------------

  let lastTotal = 0;
  let currentPreset = new URLSearchParams(window.location.search).get("view") || "";
  let savedViews = [];
  const viewSelect = $("view-select");

  async function loadViews(selectValue) {
    const resp = await fetch(api("/views"));
    const data = await resp.json();
    savedViews = data.saved;
    viewSelect.replaceChildren(el("option", {value: "", text: "All pages"}));
    const built = el("optgroup", {label: "Built-in views"});
    data.presets.forEach((p) => built.append(el("option", {value: `preset:${p.key}`, text: p.title})));
    viewSelect.append(built);
    if (savedViews.length) {
      const saved = el("optgroup", {label: "Saved views"});
      savedViews.forEach((v) => saved.append(el("option", {value: `saved:${v.id}`, text: v.name})));
      viewSelect.append(saved);
    }
    viewSelect.value = selectValue ?? (currentPreset ? `preset:${currentPreset}` : "");
    $("delete-view").hidden = !viewSelect.value.startsWith("saved:");
  }

  function applyView(value) {
    $("delete-view").hidden = !value.startsWith("saved:");
    table.clearHeaderFilter();
    if (value.startsWith("saved:")) {
      const view = savedViews.find((v) => `saved:${v.id}` === value);
      currentPreset = view.definition.view || "";
      view.definition.filters.forEach((f) => table.setHeaderFilterValue(f.field, f.value));
      table.setSort(view.definition.sorts);
    } else {
      currentPreset = value.startsWith("preset:") ? value.slice(7) : "";
      table.clearSort();
    }
    const url = new URL(window.location.href);
    if (currentPreset && !value.startsWith("saved:")) url.searchParams.set("view", currentPreset);
    else url.searchParams.delete("view");
    history.replaceState(null, "", url);
    table.setData();
  }

  viewSelect.addEventListener("change", () => applyView(viewSelect.value));

  $("save-view").addEventListener("click", async () => {
    const name = window.prompt("Name this view (filters, sort, and built-in view are saved):");
    if (!name || !name.trim()) return;
    const definition = {
      view: currentPreset,
      filters: table.getHeaderFilters().map((f) => ({field: f.field, value: f.value})),
      sorts: table.getSorters().map((s) => ({column: s.field, dir: s.dir})),
    };
    const r = await send("POST", "/views", {name, definition});
    if (!r.ok) { status.textContent = r.data.detail || "Couldn't save the view."; return; }
    await loadViews(`saved:${r.data.id}`);
    status.textContent = `Saved view "${r.data.name}".`;
  });

  $("delete-view").addEventListener("click", async () => {
    const id = viewSelect.value.slice(6);
    const view = savedViews.find((v) => String(v.id) === id);
    if (!view || !window.confirm(`Delete the saved view "${view.name}"?`)) return;
    await send("DELETE", `/views/${id}`);
    await loadViews("");
    applyView("");
  });

  $("problem-badge").addEventListener("click", () => { viewSelect.value = "preset:problems"; applyView("preset:problems"); });

  // ---- grid ------------------------------------------------------------------

  const choices = [""].concat(DISPOSITIONS);
  const filterChoices = {"": "", "(blank)": "(blank)"};
  DISPOSITIONS.forEach((c) => { filterChoices[c] = c; });

  function problemFormatter(cell) {
    const msg = cell.getValue();
    if (!msg) return "";
    return el("span", {class: "problem-flag", title: msg, "aria-label": `Problem: ${msg}`, text: "!"});
  }

  function redirectFormatter(cell) {
    const row = cell.getRow().getData();
    if (!row.redirect_to) {
      return NEEDS_TARGET.includes(effective(row)) ? el("span", {class: "muted", text: "Choose…"}) : "";
    }
    const extra = row.content_targets.length > 1 ? ` +${row.content_targets.length - 1}` : "";
    return el("span", {class: "target"}, el("span", {text: row.redirect_label || ""}),
      el("span", {class: "muted", text: ` ${row.redirect_path || "(new page)"}${extra}`}));
  }

  const disposition = (title, field) => ({
    title, field, width: 130, editor: "list", editorParams: {values: choices},
    headerFilter: "list", headerFilterParams: {values: filterChoices}, headerFilterFunc: "=",
  });

  const table = new Tabulator("#grid", {
    ajaxURL: api("/pages"),
    ajaxParams: () => ({view: currentPreset}),
    pagination: true, paginationMode: "remote", paginationSize: 100, paginationSizeSelector: [50, 100, 250, 500],
    sortMode: "remote", filterMode: "remote",
    height: "100%", layout: "fitColumns", index: "id",
    selectableRows: true, selectableRowsRangeMode: "click",
    placeholder: "No pages match these filters",
    ajaxResponse(url, params, resp) {
      lastTotal = resp.last_row;
      const plural = resp.last_row === 1 ? "page" : "pages";
      $("grid-count").textContent = `${resp.last_row.toLocaleString()} ${plural}`;
      $("bulk-total").textContent = resp.last_row.toLocaleString();
      $("bulk-noun").textContent = plural;
      const badge = $("problem-badge");
      badge.hidden = !resp.problem_count;
      badge.textContent = `${resp.problem_count} problem${resp.problem_count === 1 ? "" : "s"}`;
      return resp;
    },
    columns: [
      {formatter: "rowSelection", titleFormatter: "rowSelection", hozAlign: "center", headerSort: false,
       width: 40, frozen: true},
      {title: "", field: "problem", formatter: problemFormatter, width: 34, hozAlign: "center", headerSort: false},
      {title: "Label", field: "label", formatter: "plaintext", headerFilter: "input", minWidth: 200},
      {title: "Path", field: "url_path", formatter: "plaintext", headerFilter: "input", minWidth: 200},
      {title: "HTTP", field: "status_code", width: 72, hozAlign: "right"},
      {title: "Sessions", field: "sessions", width: 96, hozAlign: "right", formatter: "money",
       formatterParams: {precision: 0, thousand: ","}, headerSortStartingDir: "desc"},
      {title: "Inlinks", field: "inlinks", width: 84, hozAlign: "right", headerSortStartingDir: "desc"},
      {title: "In current nav", field: "in_current_nav", width: 82, hozAlign: "center", formatter: "tickCross",
       formatterParams: {crossElement: false}},
      {title: "Collection", field: "collection", width: 104, formatter: "plaintext", headerFilter: "input"},
      {title: "Parent", field: "parent_label", formatter: "plaintext", headerFilter: "input", minWidth: 140},
      {title: "Menus", field: "menus", formatter: "plaintext", headerFilter: "input", width: 110},
      disposition("Recommendation", "recommendation"),
      disposition("Final Decision", "decision"),
      {title: "Redirect to", field: "redirect_label", formatter: redirectFormatter, headerFilter: "input",
       minWidth: 180, cellClick: (e, cell) => openDialog({row: cell.getRow()})},
      {title: "Status", field: "status", width: 84, editor: "list", editorParams: {values: ["todo", "done"]},
       headerFilter: "list", headerFilterParams: {values: {"": "", todo: "todo", done: "done"}}},
      {title: "Notes", field: "notes", formatter: "plaintext", editor: "textarea", headerFilter: "input", minWidth: 180},
    ],
  });

  table.on("rowSelectionChanged", (data) => {
    $("bulk-count").textContent = data.length ? `${data.length} selected, or` : "Select rows with the checkboxes, or";
  });

  function report(result, row) {
    if (result.ok) {
      row.update(result.data.row);
      row.reformat();
      status.textContent = result.data.warning || "Saved.";
      table.setPage(table.getPage());  // refresh problem markers on other rows
    } else if (result.data.row) {
      row.update(result.data.row);
      row.reformat();
      status.textContent = result.data.detail;
    } else {
      status.textContent = `Not saved: ${result.data.detail || result.status}`;
    }
  }

  table.on("cellEdited", async (cell) => {
    const field = cell.getField();
    const row = cell.getRow();
    const data = row.getData();
    let value = cell.getValue();
    if (value === "") value = null;
    if ((field === "recommendation" || field === "decision")) {
      const after = Object.assign({}, data, {[field]: value});
      const needs = NEEDS_TARGET.includes(effective(after));
      const missing = !data.redirect_to || (effective(after) === "split" && data.content_targets.length < 2);
      if (needs && missing) {
        openDialog({row, pending: {[field]: value}, onCancel: () => cell.restoreOldValue()});
        return;
      }
    }
    const result = await send("PATCH", `/pages/${data.id}`, {changes: {[field]: value}, updated_at: data.updated_at});
    if (!result.ok && !result.data.row) cell.restoreOldValue();
    report(result, row);
  });

  // ---- target dialog ---------------------------------------------------------

  const dialog = $("target-dialog");
  let dlg = null;  // {row, mode, redirect: {id,label,url_path}|null, content: [...], onCancel, onSave}

  function fillSelect(select, value) {
    select.replaceChildren(...choices.map((c) => el("option", {value: c, text: c || "(none)"})));
    select.value = value || "";
  }

  function chip(page, onRemove) {
    const remove = el("button", {type: "button", class: "link", "aria-label": `Remove ${page.label}`, text: "Remove"});
    remove.addEventListener("click", onRemove);
    return el("li", {}, el("span", {text: page.label}), el("span", {class: "muted", text: ` ${page.url_path || ""} `}), remove);
  }

  function renderDialog() {
    const disp = dlg.mode === "bulk" ? "combine" : ($("td-decision").value || $("td-reco").value);
    $("td-redirect-set").hidden = !["combine", "split", "delete"].includes(disp);
    $("td-content-set").hidden = disp !== "split";
    const chosen = $("td-redirect-chosen");
    if (dlg.redirect) {
      const clear = el("button", {type: "button", class: "link", text: "Clear"});
      clear.addEventListener("click", () => { dlg.redirect = null; renderDialog(); });
      chosen.replaceChildren(el("strong", {text: dlg.redirect.label}),
        el("span", {class: "muted", text: ` ${dlg.redirect.url_path || "(new page)"} `}), clear);
    } else {
      chosen.replaceChildren(el("span", {class: "muted",
        text: disp === "delete" ? "No redirect: the old URL will 404." : "Not chosen yet."}));
    }
    $("td-content-chosen").replaceChildren(...dlg.content.map((p, i) =>
      chip(p, () => { dlg.content.splice(i, 1); renderDialog(); })));
  }

  function searchBox(inputId, resultsId, onPick) {
    const input = $(inputId);
    const results = $(resultsId);
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = input.value.trim();
        if (!q) { results.replaceChildren(); return; }
        const exclude = dlg && dlg.row ? `&exclude=${dlg.row.getData().id}` : "";
        const resp = await fetch(api(`/targets?q=${encodeURIComponent(q)}${exclude}`));
        const pages = await resp.json();
        results.replaceChildren(...(pages.length ? pages.map((p) => {
          const btn = el("button", {type: "button"}, el("span", {text: p.label}),
            el("span", {class: "muted", text: ` ${p.url_path || "(new page)"}`}));
          btn.addEventListener("click", () => { onPick(p); input.value = ""; results.replaceChildren(); renderDialog(); });
          return el("li", {}, btn);
        }) : [el("li", {class: "muted", text: "No matching pages that can take a redirect."})]));
      }, 200);
    });
  }

  searchBox("td-redirect-search", "td-redirect-results", (p) => { dlg.redirect = p; });
  searchBox("td-content-search", "td-content-results", (p) => {
    if (!dlg.content.some((c) => c.id === p.id)) dlg.content.push(p);
  });
  $("td-reco").addEventListener("change", renderDialog);
  $("td-decision").addEventListener("change", renderDialog);

  async function openDialog(opts) {
    $("td-error").textContent = "";
    $("td-redirect-search").value = "";
    $("td-content-search").value = "";
    $("td-redirect-results").replaceChildren();
    $("td-content-results").replaceChildren();
    if (opts.mode === "bulk") {
      dlg = Object.assign({redirect: null, content: []}, opts);
      $("td-title").textContent = opts.title;
      $("td-path").textContent = opts.hint || "Pick the page they all redirect to.";
      $("td-dispositions").hidden = true;
    } else {
      const data = opts.row.getData();
      const pending = opts.pending || {};
      dlg = Object.assign({mode: "row", content: [], redirect: null}, opts);
      $("td-title").textContent = data.label;
      $("td-path").textContent = data.url_path || "(new page)";
      $("td-dispositions").hidden = false;
      fillSelect($("td-reco"), "recommendation" in pending ? pending.recommendation : data.recommendation);
      fillSelect($("td-decision"), "decision" in pending ? pending.decision : data.decision);
      if (data.redirect_to) dlg.redirect = {id: data.redirect_to, label: data.redirect_label, url_path: data.redirect_path};
      if (data.content_targets.length) await loadContentLabels(data.id);
    }
    renderDialog();
    dialog.showModal();
    $(dlg.mode === "bulk" || !$("td-redirect-set").hidden ? "td-redirect-search" : "td-reco").focus();
  }

  async function loadContentLabels(pageId) {
    const resp = await fetch(api(`/pages/${pageId}/content-targets`));
    if (resp.ok) dlg.content = await resp.json();
  }

  function closeDialog(cancelled) {
    if (cancelled && dlg && dlg.onCancel) dlg.onCancel();
    dlg = null;
    dialog.close();
  }

  $("td-cancel").addEventListener("click", () => closeDialog(true));
  dialog.addEventListener("cancel", (e) => { e.preventDefault(); closeDialog(true); });

  $("td-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (dlg.mode === "bulk") {
      if (!dlg.redirect) { $("td-error").textContent = "Pick the page to redirect to."; return; }
      const onSave = dlg.onSave;
      const target = dlg.redirect.id;
      closeDialog(false);
      onSave(target);
      return;
    }
    const row = dlg.row;
    const data = row.getData();
    const decision = $("td-decision").value || null;
    const recommendation = $("td-reco").value || null;
    const disp = decision || recommendation;
    const changes = {recommendation, decision, redirect_to: dlg.redirect ? dlg.redirect.id : null};
    if (disp === "split") {
      const ids = dlg.content.map((p) => p.id);
      if (dlg.redirect && !ids.includes(dlg.redirect.id)) ids.unshift(dlg.redirect.id);
      changes.content_targets = ids;
    }
    const result = await send("PATCH", `/pages/${data.id}`, {changes, updated_at: data.updated_at});
    if (result.ok || result.status === 409) {
      closeDialog(false);
      report(result, row);
    } else {
      $("td-error").textContent = result.data.detail || `Not saved (${result.status}).`;
    }
  });

  // ---- bulk ------------------------------------------------------------------

  const bulkValue = $("bulk-value");
  function fillBulkValues() {
    const field = $("bulk-field").value;
    const values = field === "status" ? ["todo", "done"]
      : field === "parent_id" ? ["pick", "top"] : field === "menu" ? ["add"]
      : ["", "keep", "delete", "review", "combine"];
    const names = {"": "(clear)", combine: "combine into…", pick: "a page I choose…", top: "the top level",
      add: "a menu I choose…"};
    bulkValue.replaceChildren(...values.map((v) => el("option", {value: v, text: names[v] || v})));
  }
  $("bulk-field").addEventListener("change", fillBulkValues);
  fillBulkValues();

  $("bulk-apply").addEventListener("click", async () => {
    const all = $("bulk-all").checked;
    const ids = table.getSelectedData().map((r) => r.id);
    if (!all && !ids.length) { status.textContent = "Select some rows first, or tick “use all matching pages”."; return; }
    const field = $("bulk-field").value;
    const fieldName = $("bulk-field").selectedOptions[0].textContent;
    const value = bulkValue.value;
    const count = all ? lastTotal : ids.length;
    const target = all
      ? {match: {view: currentPreset, filters: table.getHeaderFilters().map((f) => ({field: f.field, value: f.value}))}}
      : {ids};

    const run = async (changes) => {
      const r = await send("POST", "/pages/bulk", Object.assign({changes}, target));
      if (!r.ok) { status.textContent = r.data.detail || `Not saved (${r.status}).`; return; }
      table.deselectRow();
      $("bulk-all").checked = false;
      status.textContent = `Updated ${r.data.count} page${r.data.count === 1 ? "" : "s"}. ${r.data.warning || ""}`;
      table.setPage(table.getPage());
    };

    const pages = `${count.toLocaleString()} page${count === 1 ? "" : "s"}`;
    if (field === "menu") {
      MenuPicker.open({api, csrf, title: `Add ${pages} to a menu`, target,
        onDone: (added, menuName) => {
          table.deselectRow();
          $("bulk-all").checked = false;
          status.textContent = `Added ${added} entr${added === 1 ? "y" : "ies"} to the ${menuName}.`;
          table.setPage(table.getPage());
        }});
      return;
    }
    if (field === "parent_id" && value === "pick") {
      openDialog({mode: "bulk", title: `Move ${pages} under…`,
        hint: "Pick the page they should sit under in the new navigation. Their sub-pages move with them.",
        onSave: (parentId) => run({parent_id: parentId})});
      return;
    }
    if (field === "parent_id") {
      if (count > 25 && !window.confirm(`Move ${pages} to the top level?`)) return;
      run({parent_id: null});
      return;
    }
    if (value === "combine") {
      openDialog({mode: "bulk", title: `Combine ${count.toLocaleString()} page${count === 1 ? "" : "s"} into…`,
        onSave: (redirectTo) => run({[field]: "combine", redirect_to: redirectTo})});
      return;
    }
    if (count > 25 && !window.confirm(`Set ${fieldName} to “${value || "(clear)"}” on ${count.toLocaleString()} pages?`)) return;
    run({[field]: value || null});
  });

  loadViews();
})();
