// Rename in place: double-click a name (or use its Rename action) to edit it right there.
// Enter or clicking away saves, Esc cancels. Used by the Sitemap and Navigation tabs, list and map.
window.InlineEdit = (function () {
  "use strict";

  // opts: {value, save(text) -> Promise, allowEmpty, hint}
  function attach(label, opts) {
    label.classList.add("inline-editable");
    label.dataset.renamable = "1";
    label.title = opts.hint || "Double-click to rename";
    label._inline = opts;
    label.addEventListener("dblclick", (e) => { e.preventDefault(); e.stopPropagation(); start(label); });
  }

  function start(label) {
    const opts = label._inline;
    if (!opts || label.hidden) return;
    const input = document.createElement("input");
    input.type = "text";
    input.className = "inline-input";
    input.value = opts.value;
    input.maxLength = 200;
    input.setAttribute("aria-label", "New name");
    input.style.width = `${Math.max(label.offsetWidth + 48, 180)}px`;
    label.hidden = true;
    label.after(input);
    input.focus();
    input.select();

    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      const text = input.value.replace(/\s+/g, " ").trim();
      if (commit && text !== opts.value && (text || opts.allowEmpty)) {
        input.disabled = true;
        await opts.save(text);  // the caller redraws with the new name
      }
      input.remove();
      label.hidden = false;
    };
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();  // keep page shortcuts (undo, arrows) out of the text box
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
    ["mousedown", "click", "dblclick"].forEach((t) => input.addEventListener(t, (e) => e.stopPropagation()));
  }

  // Start editing the name of the item with this id, in whichever view is showing.
  function startFor(id) {
    const found = [...document.querySelectorAll(
      `li[data-id="${id}"] > .tree-row [data-renamable], .map-card[data-id="${id}"] > .map-head [data-renamable]`)]
      .find((n) => n.offsetParent !== null);
    if (found) start(found);
  }

  return {attach, start, startFor};
})();
