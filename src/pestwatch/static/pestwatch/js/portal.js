/* PestWatch portal shell: sidenav, page sizing and layer status polling. */
(function () {
  "use strict";

  var portal = document.getElementById("pw-portal");
  if (!portal) return;

  // --- Sidenav collapse, remembered per browser ---------------------------
  var KEY = "pestwatch.sidenav.collapsed";
  var toggle = document.getElementById("pw-sidenav-toggle");
  function setCollapsed(on) {
    portal.classList.toggle("pw-collapsed", on);
    toggle.setAttribute("aria-expanded", String(!on));
    toggle.title = on ? "Expand menu" : "Collapse menu";
    window.dispatchEvent(new Event("pestwatch:resize"));
  }
  try { setCollapsed(window.localStorage.getItem(KEY) === "1"); } catch (e) { /* storage blocked */ }
  toggle.addEventListener("click", function () {
    var on = !portal.classList.contains("pw-collapsed");
    setCollapsed(on);
    try { window.localStorage.setItem(KEY, on ? "1" : "0"); } catch (e) { /* storage blocked */ }
  });

  // --- Fill the viewport below GeoNode's header and topbar ----------------
  // The header (.gn-main-header) is fixed; the blue topbar (#gn-topbar) is a
  // separate sticky bar placed under it, so measure the lower of the two.
  var bars = [".gn-main-header", "#gn-topbar"].map(function (s) { return document.querySelector(s); })
    .filter(Boolean);
  var fullPage = document.body.classList.contains("pw-fullpage");
  function fit() {
    var top = 0;
    bars.forEach(function (bar) {
      var r = bar.getBoundingClientRect();
      if (r.height > 0) top = Math.max(top, r.bottom);
    });
    if (!fullPage) top = portal.getBoundingClientRect().top + window.pageYOffset;
    top = Math.max(0, Math.round(top));
    portal.style.setProperty("--pw-top", top + "px");
    portal.style.setProperty("--pw-height", (window.innerHeight - top) + "px");
    window.dispatchEvent(new Event("pestwatch:resize"));
  }
  window.addEventListener("resize", fit);
  window.addEventListener("load", fit);
  // The header grows when its menu wraps or a banner appears.
  if (fullPage && "ResizeObserver" in window) {
    var observer = new ResizeObserver(fit);
    bars.forEach(function (bar) { observer.observe(bar); });
  }
  fit();

  // --- Layers list: reload when a layer finishes processing ---------------
  var busyEl = document.getElementById("pw-busy-ids");
  var urlEl = document.getElementById("pw-status-url");
  if (busyEl && urlEl) {
    var ids = JSON.parse(busyEl.textContent);
    var poll = function () {
      fetch(urlEl.getAttribute("data-url") + "?ids=" + ids.join(","), {credentials: "same-origin"})
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var done = ids.some(function (id) {
            var s = data[String(id)];
            return !s || s.status === "ready" || s.status === "failed";
          });
          if (done) window.location.reload();
          else window.setTimeout(poll, 5000);
        })
        .catch(function () { window.setTimeout(poll, 15000); });
    };
    window.setTimeout(poll, 4000);
  }
})();
