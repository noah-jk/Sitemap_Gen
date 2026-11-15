(function () {
  const body = document.body;
  const slug = body.dataset.slug;
  const csrf = body.dataset.csrf;
  const choices = [""].concat(body.dataset.dispositions.split(","));
  const filterChoices = {"": "", "(blank)": "(blank)"};
  choices.slice(1).forEach(c => { filterChoices[c] = c; });
  const status = document.getElementById("grid-status");

  const disposition = (title, field) => ({
    title, field, width: 130, editor: "list", editorParams: {values: choices},
    headerFilter: "list", headerFilterParams: {values: filterChoices}, headerFilterFunc: "=",
  });

  const table = new Tabulator("#grid", {
    ajaxURL: `/api/p/${encodeURIComponent(slug)}/pages`,
    pagination: true, paginationMode: "remote", paginationSize: 100, paginationSizeSelector: [50, 100, 250, 500],
    sortMode: "remote", filterMode: "remote",
    height: "100%", layout: "fitColumns", index: "id",
    placeholder: "No pages match these filters",
    ajaxResponse(url, params, resp) {
      status.textContent = `${resp.last_row.toLocaleString()} pages`;
      return resp;
    },
    columns: [
      {title: "Label", field: "label", formatter: "plaintext", headerFilter: "input", minWidth: 220},
      {title: "Path", field: "url_path", formatter: "plaintext", headerFilter: "input", minWidth: 240},
      {title: "HTTP", field: "status_code", width: 75, hozAlign: "right"},
      {title: "Sessions", field: "sessions", width: 100, hozAlign: "right", formatter: "money",
       formatterParams: {precision: 0, thousand: ","}, headerSortStartingDir: "desc"},
      {title: "Inlinks", field: "inlinks", width: 90, hozAlign: "right", headerSortStartingDir: "desc"},
      {title: "In nav", field: "in_current_nav", width: 80, hozAlign: "center", formatter: "tickCross",
       formatterParams: {crossElement: false}},
      {title: "Collection", field: "collection", width: 110, formatter: "plaintext", headerFilter: "input"},
      disposition("Recommendation", "recommendation"),
      disposition("Final Decision", "decision"),
      {title: "Status", field: "status", width: 90, editor: "list", editorParams: {values: ["todo", "done"]},
       headerFilter: "list", headerFilterParams: {values: {"": "", todo: "todo", done: "done"}}},
      {title: "Notes", field: "notes", formatter: "plaintext", editor: "textarea", headerFilter: "input", minWidth: 200},
    ],
  });

  table.on("cellEdited", async (cell) => {
    const row = cell.getRow();
    const data = row.getData();
    let resp;
    try {
      resp = await fetch(`/api/p/${encodeURIComponent(slug)}/pages/${data.id}`, {
        method: "PATCH",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: JSON.stringify({field: cell.getField(), value: cell.getValue() || null, updated_at: data.updated_at}),
      });
    } catch (e) {
      cell.restoreOldValue();
      status.textContent = "Couldn't reach the server; change not saved.";
      return;
    }
    const result = await resp.json().catch(() => ({}));
    if (resp.ok) {
      row.update(result);
      status.textContent = "Saved.";
    } else if (resp.status === 409) {
      row.update(result.row);
      status.textContent = "Someone else changed this row first. It now shows their version.";
    } else if (resp.status === 401) {
      window.location.href = "/login";
    } else {
      cell.restoreOldValue();
      status.textContent = `Not saved: ${result.detail || resp.status}`;
    }
  });
})();
