// The Rules tab: your rule library (saved to your account), the if-then builder, preview and
// apply for this project, and where page dates come from. Everything is built with DOM nodes and
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

  // ---- the library -------------------------------------------------------------

  let sortable = null;
  function drawList() {
    const list = $("rule-list");
    list.replaceChildren(...library.map((r) => {
      const box = el("input", {type: "checkbox", "aria-label": `Run “${r.name}”`});
      box.checked = toRun.has(r.id);
      box.addEventListener("change", () => { if (box.checked) toRun.add(r.id); else toRun.delete(r.id); rememberRun(); });
      const name = el("button", {type: "button", class: "link rule-name", text: r.name});
      name.addEventListener("click", () => edit(r));
      const li = el("li", {"data-id": r.id, class: current && current.id === r.id ? "current" : ""},
        el("span", {class: "tree-handle", title: "Drag to reorder", "aria-hidden": "true", text: "⠿"}), box, name);
      return li;
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
    const hint = info.type === "path" && c.op.includes("matches")
      ? el("span", {class: "muted small", text: "* = one folder, ** = any depth"}) : "";
    return el("div", {class: "cond-row"}, fieldSel, opSel, valueEl, remove, hint);
  }

  function actionRow(index) {
    const actions = current.definition.actions;
    const a = actions[index];
    const info = schema.actions[a.field];
    const taken = new Set(actions.map((x) => x.field));
    const fieldSel = select(Object.entries(schema.actions).filter(([k]) => k === a.field || !taken.has(k))
      .map(([k, v]) => [k, v.name]), a.field, "Field to set");
    fieldSel.addEventListener("change", () => {
      actions[index] = defaultAction(fieldSel.value);
      markDirty(); drawEditor();
    });
    const kinds = info.type === "page" ? ["sitemap_parent", "url_parent", "path", "blank"]
      : ["value"].concat(info.copy_from.length ? ["field"] : [], info.append ? ["append"] : [], ["blank"]);
    const kindSel = select(kinds.map((k) => [k, schema.kinds[k]]), a.kind, "Set to");
    kindSel.addEventListener("change", () => {
      a.kind = kindSel.value;
      a.value = a.kind === "field" ? info.copy_from[0] : a.kind === "value" && info.choices ? info.choices[0] : "";
      markDirty(); drawEditor();
    });
    let valueEl = el("span");
    if (a.kind === "value" && info.choices) {
      valueEl = select(info.choices.map((v) => [v, choiceName(a.field, v)]), a.value || info.choices[0], "Value");
      a.value = valueEl.value;
    } else if (a.kind === "field") {
      valueEl = select(info.copy_from.map((f) => [f, (schema.conditions[f] || {name: f}).name]), a.value, "Copy from");
      a.value = valueEl.value;
    } else if (["value", "append", "path"].includes(a.kind)) {
      valueEl = el("input", {type: "text", "aria-label": "Value",
        placeholder: a.kind === "path" ? "/admissions" : a.kind === "append" ? "Line to add" : ""});
      valueEl.value = a.value ?? "";
    }
    const update = () => { a.value = valueEl.value; markDirty(); };
    valueEl.addEventListener("input", update);
    valueEl.addEventListener("change", update);
    const remove = el("button", {type: "button", class: "link", "aria-label": "Remove this action", text: "✕"});
    remove.addEventListener("click", () => { actions.splice(index, 1); markDirty(); drawEditor(); });
    return el("div", {class: "cond-row"}, fieldSel, el("span", {class: "muted", text: "to"}), kindSel, valueEl, remove);
  }

  function defaultAction(field) {
    const info = schema.actions[field];
    if (info.type === "page") return {field, kind: "sitemap_parent"};
    return {field, kind: "value", value: info.choices ? info.choices[0] : ""};
  }

  function drawEditor() {
    $("editor").hidden = !current;
    $("editor-empty").hidden = !!current;
    if (!current) return;
    const d = current.definition;
    $("rule-name").value = current.name;
    $("rule-mode").value = d.mode;
    $("delete-rule").hidden = current.id == null;
    const groups = $("groups");
    groups.replaceChildren(...d.groups.map((group, gi) => {
      const box = el("div", {class: "cond-group"});
      group.forEach((_, ci) => {
        if (ci > 0) box.append(el("div", {class: "joiner", text: "and"}));
        box.append(conditionRow(group, ci));
      });
      const and = el("button", {type: "button", class: "link", text: "+ and…"});
      and.addEventListener("click", () => { group.push({field: "path", op: "starts_with", value: ""}); markDirty(); drawEditor(); });
      const dropGroup = el("button", {type: "button", class: "link", text: "Remove group"});
      dropGroup.addEventListener("click", () => { d.groups.splice(gi, 1); markDirty(); drawEditor(); });
      box.append(el("div", {class: "row"}, and, d.groups.length > 1 ? dropGroup : ""));
      return gi > 0 ? el("div", {}, el("div", {class: "joiner or", text: "or"}), box) : box;
    }));
    if (!d.groups.length) groups.append(el("p", {class: "muted small", text: "No conditions: every page matches."}));
    $("actions").replaceChildren(...d.actions.map((_, i) => actionRow(i)));
    $("add-action").hidden = d.actions.length >= Object.keys(schema.actions).length;
    $("rule-dirty").hidden = !dirty;
    $("editor-error").textContent = "";
    drawList();
  }

  function edit(rule) {
    if (dirty && !window.confirm("Discard the unsaved changes to this rule?")) return;
    current = rule ? JSON.parse(JSON.stringify(rule)) : {
      id: null, name: "", definition: {mode: "include", groups: [[{field: "path", op: "starts_with", value: ""}]],
        actions: [defaultAction("recommendation")]}};
    if (!current.definition.groups.length) current.definition.groups = [];
    dirty = rule == null;
    drawEditor();
    if (!rule) $("rule-name").focus();
  }

  $("new-rule").addEventListener("click", () => edit(null));
  $("rule-name").addEventListener("input", () => { current.name = $("rule-name").value; markDirty(); });
  $("rule-mode").addEventListener("change", () => { current.definition.mode = $("rule-mode").value; markDirty(); });
  $("add-group").addEventListener("click", () => {
    current.definition.groups.push([{field: "path", op: "starts_with", value: ""}]); markDirty(); drawEditor();
  });
  $("add-action").addEventListener("click", () => {
    const taken = new Set(current.definition.actions.map((a) => a.field));
    const next = Object.keys(schema.actions).find((k) => !taken.has(k));
    if (next) { current.definition.actions.push(defaultAction(next)); markDirty(); drawEditor(); }
  });

  $("save-rule").addEventListener("click", async () => {
    const payload = {name: current.name, definition: current.definition};
    const r = current.id == null ? await send("POST", "/api/rules", payload)
      : await send("PUT", `/api/rules/${current.id}`, payload);
    if (!r.ok) { $("editor-error").textContent = r.data.detail || "Couldn't save the rule."; return; }
    if (current.id == null) { toRun.add(r.data.id); rememberRun(); }  // a new rule starts ticked
    current = null;  // saved: close the editor
    dirty = false;
    await loadLibrary();
    drawEditor();
    $("run-status").textContent = `Saved “${r.data.name}”.`;
  });

  $("delete-rule").addEventListener("click", async () => {
    if (!window.confirm(`Delete the rule “${current.name}” from your account? It's gone from every project.`)) return;
    await send("DELETE", `/api/rules/${current.id}`);
    toRun.delete(current.id); rememberRun();
    current = null; dirty = false;
    await loadLibrary();
    drawEditor();
  });

  // ---- preview and apply ------------------------------------------------------

  // The ticked rules in list order; the one being edited runs as it reads on screen, saved or not.
  function rulesToRun() {
    return library.filter((r) => toRun.has(r.id)).map((r) =>
      current && current.id === r.id ? {name: current.name, definition: current.definition} : {name: r.name, definition: r.definition});
  }

  function showResults(results, applied) {
    const box = $("results");
    box.hidden = false;
    box.replaceChildren(el("h2", {text: applied ? "Applied" : "Preview: nothing has changed yet"}),
      ...results.map((r) => {
        const head = el("p", {}, el("strong", {text: r.name}),
          el("span", {class: "muted", text: ` · matches ${r.matched.toLocaleString()} · ${applied ? "changed" : "would change"} ${r.changed.toLocaleString()}`
            + (r.skipped_count ? ` · skipped ${r.skipped_count}` : "")}));
        const parts = [head];
        if (r.undated) {
          parts.push(el("p", {class: "warn small", text: `${r.undated.toLocaleString()} page${r.undated === 1 ? "" : "s"} matched everything but the date test and were left out because they have no date. Check “Page dates” on the left.`}));
        }
        if (r.sample.length) {
          parts.push(el("table", {class: "plain"}, el("thead", {}, el("tr", {}, el("th", {text: "Page"}), el("th", {text: "Changes"}))),
            el("tbody", {}, ...r.sample.map((s) => el("tr", {},
              el("td", {}, el("span", {text: s.label}), el("div", {class: "muted small", text: s.path})),
              el("td", {text: s.changes.map((c) => `${c.field} → ${c.to}`).join("; ")}))))));
          if (r.changed > r.sample.length) parts.push(el("p", {class: "muted small", text: `…and ${(r.changed - r.sample.length).toLocaleString()} more.`}));
        }
        if (r.skipped.length) {
          parts.push(el("p", {class: "small", text: "Skipped:"}), el("ul", {class: "small"},
            ...r.skipped.map((s) => el("li", {text: `${s.label}: ${s.why}`}))));
        }
        return el("section", {class: "card result-card"}, ...parts);
      }));
    box.scrollIntoView({behavior: "smooth", block: "start"});
  }

  async function run(which, rules) {
    if (!rules.length) { $("run-status").textContent = "Tick at least one rule to run."; return; }
    $("run-status").textContent = which === "apply" ? "Applying…" : "Working out what would change…";
    const r = await send("POST", `/api/p/${slug}/rules/${which}`, {rules});
    if (!r.ok) { $("run-status").textContent = r.data.detail || "That didn't work."; return; }
    showResults(r.data.rules, which === "apply");
    if (which === "apply") {
      $("run-status").textContent = `Changed ${r.data.changed.toLocaleString()} page${r.data.changed === 1 ? "" : "s"}.`;
      if (window.AppHistory) AppHistory.done($("run-status").textContent);
      loadContext();
    } else {
      $("run-status").textContent = "";
    }
  }

  $("preview-checked").addEventListener("click", () => run("preview", rulesToRun()));
  $("apply-checked").addEventListener("click", () => run("apply", rulesToRun()));
  $("preview-rule").addEventListener("click", () => run("preview", [{name: current.name || "This rule", definition: current.definition}]));

  // ---- page dates ---------------------------------------------------------------

  let dateSources = [];
  let sourceNames = {};
  const sourceName = (s) => sourceNames[s] || (s.startsWith("crawl:") ? `Crawl column “${s.slice(6)}”` : s);

  function drawDates(coverage) {
    $("date-sources").replaceChildren(...dateSources.map((s, i) => {
      const up = el("button", {type: "button", class: "link", "aria-label": "Try earlier", text: "↑"});
      up.addEventListener("click", () => { [dateSources[i - 1], dateSources[i]] = [dateSources[i], dateSources[i - 1]]; saveDates(); });
      const remove = el("button", {type: "button", class: "link", "aria-label": "Remove", text: "✕"});
      remove.addEventListener("click", () => { dateSources.splice(i, 1); saveDates(); });
      return el("li", {}, el("span", {text: sourceName(s)}), i > 0 ? up : "", remove);
    }));
    const add = $("date-add");
    const options = Object.keys(sourceNames).concat(crawlColumns.map((c) => `crawl:${c}`)).filter((s) => !dateSources.includes(s));
    add.replaceChildren(el("option", {value: "", text: "Choose…"}), ...options.map((s) => el("option", {value: s, text: sourceName(s)})));
    if (coverage) {
      const c = coverage;
      $("date-coverage").textContent = `${c.collection_dated.toLocaleString()} of ${c.collection_pages.toLocaleString()} news and calendar pages have a date `
        + `(${c.dated.toLocaleString()} of ${c.pages.toLocaleString()} pages in all).`;
    }
  }

  async function saveDates() {
    const r = await send("POST", `/api/p/${slug}/dates`, {sources: dateSources});
    if (r.ok) { dateSources = r.data.date_sources; drawDates(r.data.coverage); }
  }
  $("date-add").addEventListener("change", () => { if ($("date-add").value) { dateSources.push($("date-add").value); saveDates(); } });

  async function loadContext() {
    const data = await (await fetch(`/api/p/${slug}/rules/context`)).json();
    crawlColumns = data.crawl_columns;
    dateSources = data.date_sources;
    sourceNames = data.source_names;
    drawDates(data.coverage);
  }

  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("history:changed", (e) => { e.preventDefault(); loadContext(); });

  (async () => {
    await Promise.all([loadLibrary(), loadContext()]);
    drawEditor();
  })();
})();
