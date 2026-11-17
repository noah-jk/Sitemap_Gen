(function () {
  "use strict";
  // Preview pages render when asked for; keep the "Loading…" note up until the frame has loaded.
  const frame = document.getElementById("preview");
  const note = document.getElementById("building");
  const status = document.getElementById("proto-status");
  const form = document.getElementById("proto-form");
  let version = 0;
  frame.addEventListener("load", () => { note.hidden = true; });
  frame.src = frame.dataset.src;  // set after listening, so a fast load can't be missed

  function reloadPreview() {
    note.hidden = false;
    version += 1;
    frame.src = `${frame.dataset.src}?v=${version}`;  // back to the home page, rebuilt with the new settings
  }

  // Settings save as they change; one save at a time, and the latest change always gets saved.
  let saving = false;
  let again = false;
  async function save() {
    if (saving) { again = true; return; }
    const label = form.elements.version_label;
    if (!label.checkValidity()) { status.textContent = "Version label: letters, numbers, dots, dashes, and underscores only."; return; }
    saving = true;
    status.textContent = "Saving…";
    try {
      const resp = await fetch(form.action, {method: "POST", body: new FormData(form), headers: {"X-Requested-With": "fetch"}});
      if (resp.status === 401) { window.location.href = "/login"; return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        status.textContent = data.detail || "Not saved. Reload the page and try again.";
      } else {
        status.textContent = "Saved. Reloading the preview…";
        document.getElementById("zip-version").textContent = data.version_label;
        label.value = data.version_label;
        reloadPreview();
      }
    } catch (e) {
      status.textContent = "Couldn't reach the server. Nothing was saved.";
    } finally {
      saving = false;
      if (again) { again = false; save(); }
    }
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); save(); });
  form.addEventListener("change", (e) => {
    if (e.target.name === "dropdown_depth") form.querySelector('input[name=menu][value=dropdown]').checked = true;
    save();
  });
})();
