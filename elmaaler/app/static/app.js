// Live-opdatering af effekt og filtrering af tabeller. Ingen eksterne biblioteker.
(function () {
  function dk(x, d) {
    return (x == null ? "–" : Number(x).toLocaleString("da-DK", { minimumFractionDigits: d, maximumFractionDigits: d }));
  }
  var live = document.querySelector("[data-live]");
  if (live) {
    var url = live.getAttribute("data-live");
    function opdater() {
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (!j) return;
        Object.keys(j.maalere).forEach(function (id) {
          var m = j.maalere[id];
          document.querySelectorAll('[data-effekt="' + id + '"]').forEach(function (el) {
            el.textContent = m.online ? dk(m.effekt, 0) : "–";
          });
          document.querySelectorAll('[data-status="' + id + '"]').forEach(function (el) {
            el.className = "badge " + (m.online ? "online" : "offline");
            el.textContent = m.online ? "online" : "offline";
          });
          document.querySelectorAll('[data-sidst="' + id + '"]').forEach(function (el) { el.textContent = m.sidst; });
        });
        document.querySelectorAll("[data-total-kw]").forEach(function (el) { el.textContent = dk(j.total_kw, 2); });
      }).catch(function () {});
    }
    setInterval(opdater, 15000);
  }
  document.querySelectorAll("input[data-filter]").forEach(function (inp) {
    var tabel = document.getElementById(inp.getAttribute("data-filter"));
    inp.addEventListener("input", function () {
      var q = inp.value.toLowerCase().trim();
      tabel.querySelectorAll("tbody tr").forEach(function (tr) {
        tr.style.display = !q || tr.textContent.toLowerCase().indexOf(q) >= 0 ? "" : "none";
      });
    });
  });
  document.querySelectorAll("select[data-vis-filter]").forEach(function (sel) {
    var tabel = document.getElementById(sel.getAttribute("data-vis-filter"));
    sel.addEventListener("change", function () {
      var v = sel.value;
      tabel.querySelectorAll("tbody tr").forEach(function (tr) {
        tr.style.display = !v || (tr.getAttribute("data-tags") || "").indexOf(v) >= 0 ? "" : "none";
      });
    });
  });
  document.querySelectorAll("form[data-bekraeft]").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!confirm(f.getAttribute("data-bekraeft"))) e.preventDefault(); });
  });
})();
