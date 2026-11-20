/* Enhancement script (presentation and resilience only; no business logic).
   - Poster images: lazy loading, shimmer while loading, placeholder when a poster is missing.
   - Alerts: dismiss automatically after a short delay, with a manual close button.
   Loaded with `defer`, so the DOM is ready when this runs. */
(function () {
  "use strict";

  var PLACEHOLDER = "/static/img/poster-placeholder.svg";

  function prepareImage(img, index) {
    // Lazy-load every image except the first few, which are usually above the fold.
    if (!img.hasAttribute("loading") && index > 3) {
      img.setAttribute("loading", "lazy");
    }
    img.setAttribute("decoding", "async");

    var isPoster = img.classList.contains("card-img-top") || img.classList.contains("movie-poster");
    if (isPoster && !img.complete) {
      img.classList.add("poster-loading");
    }

    img.addEventListener("load", function () {
      img.classList.remove("poster-loading");
    });

    img.addEventListener("error", function () {
      if (img.getAttribute("data-fallback-applied") === "1") {
        return; // the placeholder itself failed; stop here to avoid a loop
      }
      img.setAttribute("data-fallback-applied", "1");
      img.classList.remove("poster-loading");
      img.src = PLACEHOLDER;
    });
  }

  function prepareAlerts() {
    var alerts = document.querySelectorAll(".alert");
    alerts.forEach(function (alert) {
      // Keep error alerts visible longer than informational ones.
      var delay = alert.classList.contains("alert-danger") ? 12000 : 6000;
      window.setTimeout(function () {
        alert.style.opacity = "0";
        window.setTimeout(function () {
          alert.style.display = "none";
        }, 400);
      }, delay);
    });
  }

  function init() {
    var images = document.querySelectorAll("img");
    Array.prototype.forEach.call(images, prepareImage);
    prepareAlerts();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
