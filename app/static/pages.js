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

  // The path links to the page on the live site, in a new tab. Only http(s) addresses from the
  // crawl become links; a click on the link doesn't select the row.
  function pathFormatter(cell) {
    const path = cell.getValue();
    if (!path) return el("span", {class: "muted", text: "(new page)"});
    const url = cell.getData().current_url || "";
    if (!/^https?:\/\//i.test(url)) return document.createTextNode(path);  // never a string: Tabulator would parse it as HTML
    const a = el("a", {href: url, target: "_blank", rel: "noopener noreferrer", class: "live-link",
      title: `Open ${url} in a new tab`, text: path});
    a.addEventListener("click", (e) => e.stopPropagation());
    return a;
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
      if (Array.isArray(view.definition.hidden)) setHidden(view.definition.hidden);
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
    const name = window.prompt("Name this view (filters, sort, columns, and built-in view are saved):");
    if (!name || !name.trim()) return;
    const definition = {
      view: currentPreset,
      filters: table.getHeaderFilters().map((f) => ({field: f.field, value: f.value})),
      sorts: table.getSorters().map((s) => ({column: s.field, dir: s.dir})),
      hidden: hiddenFields(),
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
  const TYPE_CHOICES = {"": "Auto", landing: "Landing", listing: "Listing", list: "List", article: "Article", form: "Form"};
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

  // Clicking a label opens the detail panel instead of selecting the row (the checkbox selects).
  // The listener sits on the label itself so the click never reaches the row's selection handler.
  function labelFormatter(cell) {
    const span = el("span", {class: "label-open", title: "Open the details", text: cell.getValue() || ""});
    span.addEventListener("click", (e) => { e.stopPropagation(); openDetail(cell.getRow()); });
    return span;
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
      {title: "", field: "problem", formatter: problemFormatter, width: 34, hozAlign: "center", headerSort: false,
       frozen: true},
      {title: "Label", field: "label", formatter: labelFormatter, headerFilter: "input", minWidth: 200, width: 240,
       frozen: true},
      {title: "Path", field: "url_path", formatter: pathFormatter, headerFilter: "input", minWidth: 200},
      {title: "HTTP", field: "status_code", width: 72, hozAlign: "right"},
      {title: "Sessions", field: "sessions", width: 96, hozAlign: "right", formatter: "money",
       formatterParams: {precision: 0, thousand: ","}, headerSortStartingDir: "desc"},
      {title: "Inlinks", field: "inlinks", width: 84, hozAlign: "right", headerSortStartingDir: "desc"},
      {title: "Words", field: "word_count", width: 80, hozAlign: "right",
       headerTooltip: "Screaming Frog's word count for the page's main content"},
      {title: "In current nav", field: "in_current_nav", width: 82, hozAlign: "center", formatter: "tickCross",
       formatterParams: {crossElement: false}},
      {title: "Collection", field: "collection", width: 104, formatter: "plaintext", headerFilter: "input"},
      {title: "Parent", field: "parent_label", formatter: "plaintext", headerFilter: "input", minWidth: 140},
      {title: "Menus", field: "menus", formatter: "plaintext", headerFilter: "input", width: 110},
      {title: "Page type", field: "page_type", width: 110, editor: "list", editorParams: {values: TYPE_CHOICES},
       formatter: (cell) => cell.getValue() ? TYPE_CHOICES[cell.getValue()] : el("span", {class: "muted", text: "Auto"}),
       headerFilter: "list", headerFilterParams: {values: Object.assign({"": "", "(blank)": "Auto"},
         Object.fromEntries(Object.entries(TYPE_CHOICES).filter(([k]) => k)))}, headerFilterFunc: "="},
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
    if (detailId === row.getData().id) loadDetail(detailId);
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

  // A combine or split without its target(s) yet goes through the redirect dialog first.
  function needsTargets(data, field, value) {
    if (field !== "recommendation" && field !== "decision") return false;
    const after = Object.assign({}, data, {[field]: value});
    const needs = NEEDS_TARGET.includes(effective(after));
    return needs && (!data.redirect_to || (effective(after) === "split" && data.content_targets.length < 2));
  }

  table.on("cellEdited", async (cell) => {
    const field = cell.getField();
    const row = cell.getRow();
    const data = row.getData();
    let value = cell.getValue();
    if (value === "") value = null;
    if (needsTargets(data, field, value)) {
      openDialog({row, pending: {[field]: value}, onCancel: () => cell.restoreOldValue()});
      return;
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
      : field === "page_type" ? Object.keys(TYPE_CHOICES)
      : ["", "keep", "delete", "review", "combine"];
    const names = Object.assign({"": "(clear)", combine: "combine into…", pick: "a page I choose…",
      top: "the top level", add: "a menu I choose…"}, field === "page_type" ? TYPE_CHOICES : {});
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

  // ---- columns ---------------------------------------------------------------

  const HIDE_KEY = `grid-hidden-${body.dataset.slug}`;
  const choosable = () => table.getColumns().filter((c) => {
    const d = c.getDefinition();
    return d.field && d.title && !d.frozen;
  });
  const hiddenFields = () => choosable().filter((c) => !c.isVisible()).map((c) => c.getField());

  function setHidden(fields, remember = true) {
    choosable().forEach((c) => {
      const hide = fields.includes(c.getField());
      if (hide && c.isVisible()) {
        if (c.getDefinition().headerFilter) table.setHeaderFilterValue(c.getField(), "");  // no invisible filters
        c.hide();
      } else if (!hide && !c.isVisible()) {
        c.show();
      }
    });
    if (remember) {
      try { localStorage.setItem(HIDE_KEY, JSON.stringify(hiddenFields())); } catch (e) { /* not remembered */ }
    }
    drawColumnList();
  }

  function drawColumnList() {
    $("col-list").replaceChildren(...choosable().map((c) => {
      const box = el("input", {type: "checkbox"});
      box.checked = c.isVisible();
      box.addEventListener("change", () => {
        const hidden = hiddenFields().filter((f) => f !== c.getField());
        if (!box.checked) hidden.push(c.getField());
        setHidden(hidden);
      });
      return el("label", {class: "check"}, box, ` ${c.getDefinition().title}`);
    }));
  }
  $("col-all").addEventListener("click", () => setHidden([]));
  const colMenu = $("col-menu");
  document.addEventListener("click", (e) => { if (colMenu.open && !colMenu.contains(e.target)) colMenu.open = false; });

  table.on("tableBuilt", () => {
    let saved = [];
    try { saved = JSON.parse(localStorage.getItem(HIDE_KEY) || "[]"); } catch (e) { saved = []; }
    setHidden(Array.isArray(saved) ? saved : [], false);
  });

  // ---- detail panel ----------------------------------------------------------

  const drawer = $("detail");
  let detailId = null;
  let detailRow = null;

  const fmt = (v) => (v === null || v === undefined || v === "") ? "—" : typeof v === "number" ? v.toLocaleString() : String(v);
  const pageText = (n) => `${n.toLocaleString()} page${n === 1 ? "" : "s"}`;

  function section(title, ...children) {
    return el("section", {class: "dt-section"}, el("h3", {text: title}), ...children);
  }

  function facts(pairs) {
    const dl = el("dl", {class: "facts"});
    pairs.filter(Boolean).forEach(([k, v, cls]) => {
      dl.append(el("dt", {text: k}), v instanceof Node ? el("dd", {class: cls || ""}, v) : el("dd", {class: cls || "", text: fmt(v)}));
    });
    return dl;
  }

  function pageLink(p) {
    const b = el("button", {type: "button", class: "link", text: p.label});
    b.addEventListener("click", () => openDetail(table.getRow(p.id) || null, p.id));
    return el("li", {}, b, el("span", {class: "muted small", text: ` ${p.url_path || "(new page)"}${p.disposition ? " · " + p.disposition : ""}`}));
  }

  function render(d) {
    $("dt-title").textContent = d.label;
    $("dt-trail").textContent = d.sitemap ? ["Home"].concat(d.trail.map((t) => t.label)).join(" › ")
      : "Not in the new sitemap (removed, gone, or a news/calendar item)";
    const url = $("dt-url");
    url.replaceChildren();
    if (/^https?:\/\//i.test(d.current_url || "")) {
      url.append(el("a", {href: d.current_url, target: "_blank", rel: "noopener noreferrer", class: "live-link", text: d.url_path}));
    } else {
      url.append(el("span", {class: "muted", text: d.is_new ? "New page (not on the current site)" : (d.url_path || "")}));
    }

    const parts = [];
    if (d.problem) parts.push(el("p", {class: "dt-problem", role: "alert"}, el("span", {class: "problem-flag", text: "!"}), ` ${d.problem}`));

    // Decision: every value is edited right where it shows.
    const row = rowFor(d);
    const edit = (field, value, options) => {
      const select = el("select", {"aria-label": FIELD_TITLES[field]});
      Object.entries(options).forEach(([v, text]) => select.append(el("option", {value: v, text})));
      select.value = value || "";
      select.addEventListener("change", () => changeField(row, field, select.value || null, select, value || ""));
      return select;
    };
    const dispOptions = Object.fromEntries(choices.map((c) => [c, c || "—"]));
    const autoType = d.sitemap ? `Auto (${TYPE_CHOICES[d.sitemap.type]})` : "Auto";
    const typeOptions = Object.assign({"": autoType}, Object.fromEntries(Object.entries(TYPE_CHOICES).filter(([k]) => k)));
    const disp = d.decision || d.recommendation || "";
    const targetButton = (text, title) => {
      const b = el("button", {type: "button", class: "value-btn", title, text});
      b.addEventListener("click", () => openDialog({row}));
      return b;
    };
    const pairs = [
      ["Recommendation", edit("recommendation", d.recommendation, dispOptions)],
      ["Final Decision", edit("decision", d.decision, dispOptions)],
    ];
    if (d.redirect_label || ["combine", "split", "delete"].includes(disp)) {
      pairs.push(["Redirect to", d.redirect_label
        ? targetButton(`${d.redirect_label} ${d.redirect_path || "(new page)"} ✎`, "Change where this page redirects")
        : targetButton(disp === "delete" ? "None: the old URL will 404. Choose… ✎" : "Choose… ✎", "Choose the page to redirect to"),
      !d.redirect_label && NEEDS_TARGET.includes(disp) ? "warn-text" : ""]);
    }
    if (disp === "split" || d.content_targets.length) {
      pairs.push(["Content goes to", targetButton(d.content_targets.length
        ? `${d.content_targets.map((t) => t.label).join(", ")} ✎` : "Choose… ✎", "Change which pages receive the content")]);
    }
    pairs.push(["Status", edit("status", d.status, {todo: "todo", done: "done"})]);
    pairs.push(["Page type", edit("page_type", d.page_type, typeOptions)]);
    parts.push(section("Decision", facts(pairs)));

    // Notes: saved with the button, Ctrl/Cmd+Enter, or when you click away.
    const notes = el("textarea", {rows: "4", "aria-label": "Notes"});
    notes.value = d.notes || "";
    const save = el("button", {type: "button", class: "primary", text: "Save notes"});
    const saved = el("span", {class: "muted small", role: "status"});
    const saveNotes = async () => {
      if (notes.value === (d.notes || "")) return;
      const r = await send("PATCH", `/pages/${d.id}`, {changes: {notes: notes.value}, updated_at: row.getData().updated_at});
      if (r.ok) {
        d.notes = notes.value;
        saved.textContent = "Saved.";
        row.update(r.data.row);
        row.reformat();
        loadDetail(d.id);
      } else {
        saved.textContent = r.data.detail || `Not saved (${r.status}).`;
      }
    };
    save.addEventListener("click", saveNotes);
    notes.addEventListener("blur", saveNotes);
    notes.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveNotes(); } });
    parts.push(section("Notes", notes, el("div", {class: "row"}, save, saved)));

    // Structure
    const s = d.sitemap;
    parts.push(section("In the new site", facts([
      ["Parent", d.parent_label || (s ? "Top level" : "—")],
      s && ["Below it", s.descendants ? `${pageText(s.descendants)} (${s.children} directly)` : "None"],
      ["Menus", d.menu_entries.length ? d.menu_entries.map((m) => m.label ? `${m.menu} (as "${m.label}")` : m.menu).join(", ") : "Not in a menu"],
    ]), d.incoming.length ? el("div", {}, el("p", {class: "small muted", text: `Absorbs ${pageText(d.incoming.length)}`}),
      el("ul", {class: "dt-list"}, ...d.incoming.map(pageLink))) : ""));

    // Crawl
    if (!d.is_new) {
      const canonicalOff = d.canonical && d.current_url && d.canonical !== d.current_url;
      parts.push(section("Crawl", facts([
        ["Title", d.title], ["H1", d.h1], ["Meta description", d.meta_description],
        ["Canonical", canonicalOff ? `${d.canonical} (differs from the address)` : d.canonical, canonicalOff ? "warn-text" : ""],
        ["HTTP status", d.status_code], ["Indexability", d.indexability], ["Crawl depth", d.crawl_depth],
        ["Inlinks", d.inlinks !== null ? `${fmt(d.inlinks)} (${fmt(d.unique_inlinks)} unique)` : null],
        ["Linked from", d.pct_of_total !== null ? `${d.pct_of_total}% of pages${d.in_current_nav ? " (in the current navigation)" : ""}` : null],
        ["Words", d.word_count], ["Near duplicates", d.near_duplicates ? `${d.near_duplicates}, closest ${d.closest_near_duplicate}` : d.near_duplicates],
        ["Collection", d.collection], d.missing_from_crawl && ["Latest crawl", "Missing from the latest crawl", "warn-text"],
      ])));
      parts.push(section("Traffic (GA4)", facts([
        ["Sessions", d.sessions], ["Views", d.views], ["Engaged sessions", d.engaged_sessions],
        ["Engagement rate", d.engagement_rate !== null ? `${Math.round(d.engagement_rate * 100)}%` : null], ["Key events", d.key_events],
      ])));
      const urls = [];
      if (d.variants.length) urls.push(el("p", {class: "small muted", text: "Also crawled as"}), el("ul", {class: "dt-list small"}, ...d.variants.map((v) => el("li", {text: v}))));
      if (d.old_redirects.length) urls.push(el("p", {class: "small muted", text: "Old URLs already redirecting here"}),
        el("ul", {class: "dt-list small"}, ...d.old_redirects.map((r) => el("li", {text: `${r.source_url} (${r.status_code})`}))));
      if (urls.length) parts.push(section("Other addresses", ...urls));
      const extra = Object.entries(d.crawl_extra);
      if (extra.length) {
        const more = el("details", {class: "dt-more"}, el("summary", {text: `All other crawl columns (${extra.length})`}), facts(extra));
        parts.push(more);
      }
    }
    $("dt-body").replaceChildren(...parts);
    $("dt-prev").disabled = !neighbour(-1);
    $("dt-next").disabled = !neighbour(1);
  }

  const FIELD_TITLES = {recommendation: "Recommendation", decision: "Final Decision", status: "Status", page_type: "Page type"};

  // The grid row for the open page, or a stand-in when the page isn't on the grid's current page.
  function rowFor(d) {
    if (detailRow && detailRow.getData().id === d.id) return detailRow;
    let data = d.row;
    return {getData: () => data, update: (fresh) => { data = Object.assign({}, data, fresh); }, reformat() {}, getElement: () => null};
  }

  async function changeField(row, field, value, select, old) {
    const data = row.getData();
    if (needsTargets(data, field, value)) {
      openDialog({row, pending: {[field]: value}, onCancel: () => { select.value = old; }});
      return;
    }
    select.disabled = true;
    const result = await send("PATCH", `/pages/${data.id}`, {changes: {[field]: value}, updated_at: data.updated_at});
    select.disabled = false;
    if (!result.ok && !result.data.row) select.value = old;
    report(result, row);
  }

  async function loadDetail(id) {
    const resp = await fetch(api(`/pages/${id}/detail`));
    if (detailId !== id) return;  // moved on meanwhile
    if (!resp.ok) { $("dt-body").replaceChildren(el("p", {class: "error", text: "That page no longer exists."})); return; }
    const d = await resp.json();
    if (detailId === id) render(d);
  }

  function markCurrent() {
    document.querySelectorAll(".tabulator-row.row-current").forEach((r) => r.classList.remove("row-current"));
    if (detailRow && detailRow.getElement()) detailRow.getElement().classList.add("row-current");
  }

  function openDetail(row, id) {
    detailRow = row || null;
    detailId = row ? row.getData().id : id;
    const top = document.querySelector(".topbar").getBoundingClientRect().bottom;
    drawer.style.top = `${Math.max(0, top)}px`;
    drawer.hidden = false;
    document.body.classList.add("drawer-open");
    markCurrent();
    $("dt-body").replaceChildren(el("p", {class: "muted", text: "Loading…"}));
    loadDetail(detailId);
  }

  function closeDetail() {
    drawer.hidden = true;
    document.body.classList.remove("drawer-open");
    detailId = null;
    detailRow = null;
    markCurrent();
  }

  function neighbour(step) {
    if (!detailRow) return null;
    const rows = table.getRows("active");
    const at = rows.indexOf(detailRow);
    return at < 0 ? null : rows[at + step] || null;
  }

  function go(step) {
    const next = neighbour(step);
    if (next) { openDetail(next); table.scrollToRow(next, "nearest", false); }
  }

  $("dt-prev").addEventListener("click", () => go(-1));
  $("dt-next").addEventListener("click", () => go(1));
  $("dt-close").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (e) => {
    if (drawer.hidden || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest("input, textarea, select, [contenteditable], .tabulator-editing") || document.querySelector("dialog[open]")) return;
    if (e.key === "Escape") closeDetail();
    else if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); go(1); }
    else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); go(-1); }
  });
  table.on("dataProcessed", () => {  // new page of rows: keep the panel on the same page if it's still listed
    if (detailId === null) return;
    detailRow = table.getRow(detailId) || null;
    markCurrent();
    if (!drawer.hidden) { $("dt-prev").disabled = !neighbour(-1); $("dt-next").disabled = !neighbour(1); }
  });
  table.on("renderComplete", markCurrent);

  loadViews();
  // Undo or redo in the top bar: reload this tab's data in place instead of the whole page.
  document.addEventListener("history:changed", (e) => {
    e.preventDefault();
    table.replaceData();
    if (detailId !== null) loadDetail(detailId);
  });
})();
