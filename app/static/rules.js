// The Rules tab: your rule list (saved to your account), with the if-then builder, previews, and
// where page dates come from each in a dialog that closes when you're done. Everything is built with DOM nodes and
// textContent; nothing from a rule or a page is ever inserted as HTML.
(function () {
  "use strict";
  const body = document.body;
  const slug = encodeURIComponent(body.dataset.slug);
  const csrf = body.dataset.csrf;
  const $ = (id) => document.getElementById(id);
  const RUN_KEY = `rules-run-${body.dataset.slug}`;

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
      resp = await fetch(path, {method, headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: payload === undefined ? undefined : JSON.stringify(payload)});
    } catch (e) {
      return {ok: false, data: {detail: "Couldn't reach the server. Nothing was changed."}};
    }
    if (resp.status === 401) window.location.href = "/login";
    return {ok: resp.ok, status: resp.status, data: await resp.json().catch(() => ({}))};
  }

  let schema = null;        // what conditions and actions are available
  let library = [];         // [{id, name, definition}]
  let crawlColumns = [];
  let current = null;       // {id|null, name, definition} being edited
  let dirty = false;
  let toRun = new Set();
  try { toRun = new Set(JSON.parse(localStorage.getItem(RUN_KEY) || "[]")); } catch (e) { /* private mode */ }
  const rememberRun = () => { try { localStorage.setItem(RUN_KEY, JSON.stringify([...toRun])); } catch (e) { /* ignore */ } };

  // ---- the editor --------------------------------------------------------------

  const conditionFields = () => {
    const out = Object.entries(schema.conditions).map(([k, v]) => [k, v.name, v.type, v.choices]);
    crawlColumns.forEach((c) => out.push([`crawl:${c}`, c, "text", null]));
    return out;
  };
  const fieldInfo = (name) => {
    if (schema.conditions[name]) return schema.conditions[name];
    return {name: name.replace(/^crawl:/, ""), type: "text", choices: null};
  };
  const choiceName = (field, value) => (field === "page_type" ? schema.type_names[value] : value) || value;

  function markDirty() {
    dirty = true;
    $("rule-dirty").hidden = false;
  }

  function select(options, value, label) {
    const s = el("select", {"aria-label": label});
    options.forEach(([v, text]) => s.append(el("option", {value: v, text})));
    if (value !== undefined && value !== null) s.value = value;
    return s;
  }

  function conditionRow(group, index) {
    const c = group[index];
    const info = fieldInfo(c.field);
    const fields = conditionFields();
    const fieldSel = el("select", {"aria-label": "Field"});
    const main = el("optgroup", {label: "Page"});
    fields.filter(([k]) => !k.startsWith("crawl:")).forEach(([k, n]) => main.append(el("option", {value: k, text: n})));
    fieldSel.append(main);
    if (crawlColumns.length) {
      const crawl = el("optgroup", {label: "Other crawl columns"});
      fields.filter(([k]) => k.startsWith("crawl:")).forEach(([k, n]) => crawl.append(el("option", {value: k, text: n})));
      fieldSel.append(crawl);
    }
    fieldSel.value = c.field;
    fieldSel.addEventListener("change", () => {
      const t = fieldInfo(fieldSel.value).type;
      group[index] = {field: fieldSel.value, op: schema.ops[t][0].op, value: ""};
      markDirty(); drawEditor();
    });
    const opSel = select(schema.ops[info.type].map((o) => [o.op, o.name]), c.op, "Test");
    opSel.addEventListener("change", () => { c.op = opSel.value; markDirty(); drawEditor(); });
    let valueEl = el("span");
    if (!schema.no_value.includes(c.op)) {
      if (info.type === "choice") {
        valueEl = select(info.choices.map((v) => [v, choiceName(c.field, v)]), c.value || info.choices[0], "Value");
        c.value = valueEl.value;
      } else {
        const type = info.type === "number" ? "number" : info.type === "date" ? "date" : "text";
        const placeholder = info.type === "path" ? (c.op.includes("matches") ? "/academics/*/facilities" : "/news")
          : info.type === "date" ? "" : "";
        valueEl = el("input", {type, "aria-label": "Value", placeholder});
        valueEl.value = c.value ?? "";
      }
      const update = () => { c.value = valueEl.value; markDirty(); };
      valueEl.addEventListener("input", update);
      valueEl.addEventListener("change", update);
    }
    const remove = el("button", {type: "button", class: "link", "aria-label": "Remove this condition", text: "✕"});
    remove.addEventListener("click", () => { group.splice(index, 1); markDirty(); drawEditor(); });
    let hint = "";
    if (info.type === "path" && c.op.includes("matches")) {
      hint = el("span", {class: "muted small", text: "* = one folder, ** = any depth"});
    } else if (info.type === "date") {
      const where = el("button", {type: "button", class: "link small", text: "Where dates come from…"});
      where.addEventListener("click", openDates);
      hint = el("span", {class: "muted small"}, document.createTextNode(`${coverageText()} `), where);
    }
    return el("div", {class: "cond-row"}, fieldSel, opSel, valueEl, remove, hint);
  }

  // One action, as one sentence: the field, then one dropdown for what it becomes (values first, then
  // "the value of another field" and "blank"), plus a text box when it needs one.
  function actionRow(index) {
    const actions = current.definition.actions;
    const a = actions[index];
    const info = schema.actions[a.field];
    const sets = (x) => (x.field === "combine_into" ? [x.as || "recommendation", "redirect_to"] : [x.field]);
    const taken = new Set(actions.filter((x) => x !== a).flatMap(sets));
    const free = (k) => (k === "combine_into" ? !taken.has("redirect_to") : !taken.has(k));
    const fieldSel = select(Object.entries(schema.actions).filter(([k]) => k === a.field || free(k))
      .map(([k, v]) => [k, v.name]), a.field, "What to set");
    fieldSel.addEventListener("change", () => { actions[index] = defaultAction(fieldSel.value); markDirty(); drawEditor(); });
    const remove = el("button", {type: "button", class: "link", "aria-label": "Remove this action", text: "✕"});
    remove.addEventListener("click", () => { actions.splice(index, 1); markDirty(); drawEditor(); });

    const pageOptions = [["sitemap_parent|", schema.kinds.sitemap_parent], ["url_parent|", schema.kinds.url_parent],
      ["path|", "the page at a path…"]];
    let options;
    if (info.type === "combine") options = pageOptions;
    else if (info.type === "page") options = pageOptions.concat([["blank|", "blank"]]);
    else if (info.choices) {
      options = info.choices.map((v) => [`value|${v}`, choiceName(a.field, v)])
        .concat(info.copy_from.map((f) => [`field|${f}`, `the value of ${(schema.conditions[f] || {name: f}).name}`]), [["blank|", "blank"]]);
    } else {
      options = [["value|", "this text…"]].concat(info.append ? [["append|", "itself plus this line…"]] : [],
        info.copy_from.map((f) => [`field|${f}`, `the value of ${(schema.conditions[f] || {name: f}).name}`]), [["blank|", "blank"]]);
    }
    const encode = () => (a.kind === "value" && info.choices ? `value|${a.value}` : a.kind === "field" ? `field|${a.value}` : `${a.kind}|`);
    const toSel = select(options, encode(), info.type === "combine" ? "Page to combine into" : "Set to");
    if (toSel.value !== encode()) { toSel.selectedIndex = 0; }
    const [k0, v0] = toSel.value.split("|");
    a.kind = k0;
    if (k0 === "value" && info.choices) a.value = v0;
    if (k0 === "field") a.value = v0;
    toSel.addEventListener("change", () => {
      const [k, v] = toSel.value.split("|");
      a.kind = k;
      a.value = (k === "value" && info.choices) || k === "field" ? v : "";
      markDirty(); drawEditor();
    });

    const parts = [fieldSel, el("span", {class: "muted", text: info.type === "combine" ? "" : "→"}), toSel];
    if ((a.kind === "value" && !info.choices) || a.kind === "append" || a.kind === "path") {
      const input = el("input", {type: "text", "aria-label": "Value",
        placeholder: a.kind === "path" ? "/admissions" : a.kind === "append" ? "Line to add" : ""});
      input.value = a.value ?? "";
      const update = () => { a.value = input.value; markDirty(); };
      input.addEventListener("input", update);
      input.addEventListener("change", update);
      parts.push(input);
    }
    if (info.type === "combine") {
      const asSel = select(Object.entries(schema.combine_as), a.as || "recommendation", "Set as");
      asSel.addEventListener("change", () => { a.as = asSel.value; markDirty(); drawEditor(); });
      parts.push(el("span", {class: "muted", text: "as the"}), asSel);
    }
    parts.push(remove);
    return el("div", {class: "cond-row"}, ...parts);
  }

  function defaultAction(field) {
    const info = schema.actions[field];
    if (info.type === "combine") return {field, kind: "sitemap_parent", as: "recommendation"};
    if (info.type === "page") return {field, kind: "sitemap_parent"};
    return {field, kind: "value", value: info.choices ? info.choices[0] : ""};
  }

  function drawEditor() {
    if (!current) return;
    const d = current.definition;
    $("rd-title").textContent = current.id == null ? "New rule" : `Edit “${current.name}”`;
    $("rule-name").value = current.name;
    $("rule-mode").value = d.mode;
    const groups = $("groups");
    groups.replaceChildren(...d.groups.map((group, gi) => {
      const box = el("div", {class: "cond-group"});
      group.forEach((_, ci) => {
        if (ci > 0) box.append(el("div", {class: "joiner", text: "and"}));
        box.append(conditionRow(group, ci));
      });
      const and = el("button", {type: "button", class: "link", text: "+ and…"});
      and.addEventListener("click", () => { group.push({field: "path", op: "matches", value: ""}); markDirty(); drawEditor(); });
      const dropGroup = el("button", {type: "button", class: "link", text: "Remove group"});
      dropGroup.addEventListener("click", () => { d.groups.splice(gi, 1); markDirty(); drawEditor(); });
      box.append(el("div", {class: "row"}, and, d.groups.length > 1 ? dropGroup : ""));
      return gi > 0 ? el("div", {}, el("div", {class: "joiner or", text: "or"}), box) : box;
    }));
    if (!d.groups.length) groups.append(el("p", {class: "muted small", text: "No conditions: every page matches."}));
    $("actions").replaceChildren(...d.actions.map((_, i) => actionRow(i)));
    $("add-action").hidden = d.actions.length >= Object.keys(schema.actions).length;
    $("rule-dirty").hidden = !dirty;
  }

  // ---- the rule list ------------------------------------------------------------

  // A rule in one line: "Path matches /academics/*/facilities → Combine into its parent in the sitemap".
  function describe(d) {
    const cond = (c) => {
      const info = fieldInfo(c.field);
      const op = (schema.ops[info.type].find((o) => o.op === c.op) || {name: c.op}).name;
      const value = schema.no_value.includes(c.op) ? "" : ` ${info.type === "choice" ? choiceName(c.field, c.value) : c.value}`;
      return `${info.name} ${op}${value}`;
    };
    const where = d.groups.length ? d.groups.map((g) => g.map(cond).join(" and ")).join("; or ") : "every page";
    const act = (a) => {
      const info = schema.actions[a.field];
      const target = {sitemap_parent: "its sitemap parent", url_parent: "its URL parent", path: `the page at ${a.value}`,
        blank: "blank", append: `+ “${a.value}”`}[a.kind];
      if (a.field === "combine_into") return `Combine into ${target}, as the ${schema.combine_as[a.as]}`;
      const value = a.kind === "field" ? `the ${(schema.conditions[a.value] || {name: a.value}).name}`
        : target || (info.choices ? choiceName(a.field, a.value) : `“${a.value}”`);
      return `${info.name} → ${value}`;
    };
    return `${d.mode === "exclude" ? "All pages except " : ""}${where} → ${d.actions.map(act).join(", ")}`;
  }

  let sortable = null;
  function drawList() {
    const list = $("rule-list");
    list.replaceChildren(...library.map((r) => {
      const box = el("input", {type: "checkbox", "aria-label": `Run “${r.name}”`});
      box.checked = toRun.has(r.id);
      box.addEventListener("change", () => { if (box.checked) toRun.add(r.id); else toRun.delete(r.id); rememberRun(); });
      const name = el("button", {type: "button", class: "link rule-name", text: r.name});
      name.addEventListener("click", () => openEditor(r));
      const editBtn = el("button", {type: "button", class: "link", text: "Edit"});
      editBtn.addEventListener("click", () => openEditor(r));
      const del = el("button", {type: "button", class: "link", text: "Delete"});
      del.addEventListener("click", () => deleteRule(r));
      return el("li", {"data-id": r.id},
        el("span", {class: "tree-handle", title: "Drag to reorder", "aria-hidden": "true", text: "⠿"}), box,
        el("div", {class: "rule-text"}, name, el("div", {class: "muted small", text: describe(r.definition)})),
        el("span", {class: "rule-actions"}, editBtn, del));
    }));
    $("rule-list-empty").hidden = library.length > 0;
    if (sortable) sortable.destroy();
    sortable = Sortable.create(list, {handle: ".tree-handle", animation: 120, onEnd: async () => {
      const ids = [...list.children].map((li) => Number(li.dataset.id));
      library.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
      await send("POST", "/api/rules/order", {ids});
    }});
  }

  async function loadLibrary() {
    const data = await (await fetch("/api/rules")).json();
    schema = data.schema;
    library = data.rules;
    drawList();
  }

  async function deleteRule(rule) {
    if (!window.confirm(`Delete the rule “${rule.name}” from your account? It's gone from every project.`)) return;
    await send("DELETE", `/api/rules/${rule.id}`);
    toRun.delete(rule.id); rememberRun();
    await loadLibrary();
    $("run-status").textContent = `Deleted “${rule.name}”.`;
  }

  // ---- the rule dialog ------------------------------------------------------------

  const ruleDialog = $("rule-dialog");

  function openEditor(rule) {
    current = rule ? JSON.parse(JSON.stringify(rule)) : {
      id: null, name: "", definition: {mode: "include", groups: [[{field: "path", op: "matches", value: ""}]],
        actions: [defaultAction("recommendation")]}};
    dirty = false;
    $("editor-error").textContent = "";
    $("rule-results").replaceChildren();
    drawEditor();
    ruleDialog.showModal();
    $("rule-name").focus();
  }

  function closeEditor() {
    if (dirty && !window.confirm("Close without saving the changes to this rule?")) return false;
    ruleDialog.close();
    current = null;
    dirty = false;
    return true;
  }

  $("new-rule").addEventListener("click", () => openEditor(null));
  $("rd-close").addEventListener("click", closeEditor);
  ruleDialog.addEventListener("cancel", (e) => { e.preventDefault(); closeEditor(); });  // Esc
  $("rule-name").addEventListener("input", () => { current.name = $("rule-name").value; markDirty(); });
  $("rule-mode").addEventListener("change", () => { current.definition.mode = $("rule-mode").value; markDirty(); });
  $("add-group").addEventListener("click", () => {
    current.definition.groups.push([{field: "path", op: "matches", value: ""}]); markDirty(); drawEditor();
  });
  $("add-action").addEventListener("click", () => {
    const taken = new Set(current.definition.actions.flatMap((a) => (a.field === "combine_into" ? [a.as, "redirect_to", a.field] : [a.field])));
    const next = Object.keys(schema.actions).find((k) => !taken.has(k) && k !== "combine_into");
    if (next) { current.definition.actions.push(defaultAction(next)); markDirty(); drawEditor(); }
  });

  // Save; returns the saved rule, or null (with the reason shown in the dialog).
  async function save() {
    const payload = {name: current.name, definition: current.definition};
    const r = current.id == null ? await send("POST", "/api/rules", payload)
      : await send("PUT", `/api/rules/${current.id}`, payload);
    if (!r.ok) { $("editor-error").textContent = r.data.detail || "Couldn't save the rule."; return null; }
    if (current.id == null) { toRun.add(r.data.id); rememberRun(); }  // a new rule starts ticked
    dirty = false;
    closeEditor();
    await loadLibrary();
    return r.data;
  }

  $("save-rule").addEventListener("click", async () => {
    const saved = await save();
    if (saved) $("run-status").textContent = `Saved “${saved.name}”.`;
  });
  $("save-apply").addEventListener("click", async () => {
    const saved = await save();
    if (saved) apply([{name: saved.name, definition: saved.definition}]);
  });
  $("preview-rule").addEventListener("click", async () => {
    $("editor-error").textContent = "";
    const r = await send("POST", `/api/p/${slug}/rules/preview`, {rules: [{name: current.name || "This rule", definition: current.definition}]});
    if (!r.ok) { $("editor-error").textContent = r.data.detail || "Couldn't preview the rule."; return; }
    showResults($("rule-results"), r.data.rules, false);
    $("rule-results").scrollIntoView({behavior: "smooth", block: "nearest"});
  });

  // ---- running ticked rules ---------------------------------------------------------

  const resultsDialog = $("results-dialog");
  const ticked = () => library.filter((r) => toRun.has(r.id)).map((r) => ({name: r.name, definition: r.definition}));

  function showResults(box, results, applied) {
    box.replaceChildren(...results.map((r) => {
      const head = el("p", {}, el("strong", {text: r.name}),
        el("span", {class: "muted", text: ` · matches ${r.matched.toLocaleString()} · ${applied ? "changed" : "would change"} ${r.changed.toLocaleString()}`
          + (r.skipped_count ? ` · skipped ${r.skipped_count}` : "")}));
      const parts = [head];
      if (r.undated) {
        const where = el("button", {type: "button", class: "link", text: "Where dates come from…"});
        where.addEventListener("click", openDates);
        parts.push(el("p", {class: "warn small"}, document.createTextNode(`${r.undated.toLocaleString()} page${r.undated === 1 ? "" : "s"} `
          + "matched everything but the date test and were left out because they have no date. "), where));
      }
      if (r.sample.length) {
        parts.push(el("table", {class: "plain"}, el("thead", {}, el("tr", {}, el("th", {text: "Page"}), el("th", {text: "Changes"}))),
          el("tbody", {}, ...r.sample.map((s) => el("tr", {},
            el("td", {}, el("span", {text: s.label}), el("div", {class: "muted small", text: s.path})),
            el("td", {text: s.changes.map((c) => `${c.field} → ${c.to}`).join("; ")}))))));
        if (r.changed > r.sample.length) parts.push(el("p", {class: "muted small", text: `…and ${(r.changed - r.sample.length).toLocaleString()} more.`}));
      } else if (!applied) {
        parts.push(el("p", {class: "muted small", text: "Nothing would change."}));
      }
      if (r.skipped.length) {
        parts.push(el("p", {class: "small", text: "Skipped:"}), el("ul", {class: "small"},
          ...r.skipped.map((s) => el("li", {text: `${s.label}: ${s.why}`}))));
      }
      return el("section", {class: "result-card"}, ...parts);
    }));
  }

  async function apply(rules) {
    if (!rules.length) { $("run-status").textContent = "Tick at least one rule to run."; return; }
    $("run-status").textContent = "Applying…";
    const r = await send("POST", `/api/p/${slug}/rules/apply`, {rules});
    if (!r.ok) { $("run-status").textContent = r.data.detail || "That didn't work."; return; }
    const skipped = r.data.rules.reduce((n, x) => n + x.skipped_count, 0);
    $("run-status").textContent = `Changed ${r.data.changed.toLocaleString()} page${r.data.changed === 1 ? "" : "s"}`
      + (skipped ? ` (${skipped} skipped: preview to see why).` : ".");
    if (r.data.changed && window.AppHistory) AppHistory.done($("run-status").textContent);  // nothing changed: nothing to undo
    loadContext();
  }

  $("preview-checked").addEventListener("click", async () => {
    const rules = ticked();
    if (!rules.length) { $("run-status").textContent = "Tick at least one rule to preview."; return; }
    const r = await send("POST", `/api/p/${slug}/rules/preview`, {rules});
    if (!r.ok) { $("run-status").textContent = r.data.detail || "That didn't work."; return; }
    $("res-title").textContent = `Preview of ${rules.length} rule${rules.length === 1 ? "" : "s"}: nothing has changed yet`;
    showResults($("results-body"), r.data.rules, false);
    resultsDialog.showModal();
  });
  $("apply-checked").addEventListener("click", () => apply(ticked()));
  $("res-apply").addEventListener("click", () => { resultsDialog.close(); apply(ticked()); });
  $("res-close").addEventListener("click", () => resultsDialog.close());
  $("res-close-x").addEventListener("click", () => resultsDialog.close());

  // ---- page dates (a small dialog, opened from date conditions and results) ---------------

  let dateSources = [];
  let sourceNames = {};
  let coverage = null;
  const sourceName = (s) => sourceNames[s] || (s.startsWith("crawl:") ? `Crawl column “${s.slice(6)}”` : s);
  const coverageText = () => (coverage
    ? `${coverage.collection_dated.toLocaleString()} of ${coverage.collection_pages.toLocaleString()} news and calendar pages have a date.` : "");

  function drawDates() {
    $("date-sources").replaceChildren(...dateSources.map((s, i) => {
      const up = el("button", {type: "button", class: "link", "aria-label": "Try earlier", text: "↑"});
      up.addEventListener("click", () => { [dateSources[i - 1], dateSources[i]] = [dateSources[i], dateSources[i - 1]]; saveDates(); });
      const remove = el("button", {type: "button", class: "link", "aria-label": "Remove", text: "✕"});
      remove.addEventListener("click", () => { dateSources.splice(i, 1); saveDates(); });
      return el("li", {}, el("span", {text: sourceName(s)}), i > 0 ? up : "", remove);
    }));
    const options = Object.keys(sourceNames).concat(crawlColumns.map((c) => `crawl:${c}`)).filter((s) => !dateSources.includes(s));
    $("date-add").replaceChildren(el("option", {value: "", text: "Choose…"}), ...options.map((s) => el("option", {value: s, text: sourceName(s)})));
    if (coverage) {
      $("date-coverage").textContent = `${coverageText()} (${coverage.dated.toLocaleString()} of ${coverage.pages.toLocaleString()} pages in all.)`;
    }
  }

  function openDates() {
    drawDates();
    $("dates-dialog").showModal();
  }
  $("dates-close").addEventListener("click", () => $("dates-dialog").close());
  $("dates-dialog").addEventListener("close", () => { if (current) drawEditor(); });

  async function saveDates() {
    const r = await send("POST", `/api/p/${slug}/dates`, {sources: dateSources});
    if (r.ok) { dateSources = r.data.date_sources; coverage = r.data.coverage; drawDates(); }
  }
  $("date-add").addEventListener("change", () => { if ($("date-add").value) { dateSources.push($("date-add").value); saveDates(); } });

  async function loadContext() {
    const data = await (await fetch(`/api/p/${slug}/rules/context`)).json();
    crawlColumns = data.crawl_columns;
    dateSources = data.date_sources;
    sourceNames = data.source_names;
    coverage = data.coverage;
  }

  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("history:changed", (e) => { e.preventDefault(); loadContext(); });

  (async () => {
    await Promise.all([loadLibrary(), loadContext()]);
  })();
})();
