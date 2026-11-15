(function () {
  "use strict";
  // The preview frame is built on request; keep the "Building…" note up until it has loaded.
  const frame = document.getElementById("preview");
  const note = document.getElementById("building");
  frame.addEventListener("load", () => { note.hidden = true; });
  frame.src = frame.dataset.src;  // set after listening, so a fast load can't be missed
})();
