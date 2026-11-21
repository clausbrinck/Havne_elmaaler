// Live-opdatering af effekt og filtrering af tabeller. Ingen eksterne biblioteker.
(function () {
  function dk(x, d) {
    return (x == null ? "–" : Number(x).toLocaleString("da-DK", { minimumFractionDigits: d, maximumFractionDigits: d }));
  }
  // Ampere (ca., ved 230 V) af standerens 16 A og en forklaring i almindelige ord
  function effektInfo(el, w) {
    el.innerHTML = "";
    if (w == null || w === "" || isNaN(w)) return;
    w = Number(w);
    var a = w / 230, pct = Math.min(a / 16 * 100, 100);
    var tekst = w < 5 ? "Intet er tændt lige nu" :
      w < 60 ? "Meget lille – fx lader eller lys" :
      w < 300 ? "Lille – fx køleskab eller lille affugter" :
      w < 800 ? "Middel – fx affugter eller frostvagt" :
      w < 2000 ? "Stort – fx varmeblæser" :
      w < 3000 ? "Meget stort – fx varmeblæser på fuldt blus" : "Tæt på sikringens grænse!";
    var amp = document.createElement("div"); amp.className = "amp";
    amp.textContent = "ca. " + dk(a, 1) + " A af 16 A";
    var bar = document.createElement("div"); bar.className = "bar lille" + (pct >= 75 ? " adv" : "");
    var fyld = document.createElement("div"); fyld.style.width = pct + "%"; bar.appendChild(fyld);
    var t = document.createElement("div"); t.className = "forklaring"; t.textContent = tekst;
    el.appendChild(amp); el.appendChild(bar); el.appendChild(t);
  }
  document.querySelectorAll("[data-effekt-info]").forEach(function (el) { effektInfo(el, el.getAttribute("data-w")); });

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
          document.querySelectorAll('[data-effekt-info="' + id + '"]').forEach(function (el) {
            effektInfo(el, m.online ? m.effekt : null);
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

// Periode-graf: Døgn / Uge / Måned / Vinter med bladring (‹ › og swipe) og tooltip ved tryk på en søjle.
(function () {
  var NS = "http://www.w3.org/2000/svg";
  function dk(x, d) {
    return Number(x).toLocaleString("da-DK", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function paen(x) {
    var e = Math.pow(10, Math.floor(Math.log10(x))), f = x / e;
    return [1, 2, 2.5, 5, 10].filter(function (s) { return s >= f - 1e-9; })[0] * e;
  }
  function kwh(v) { return dk(v, v >= 100 ? 0 : v >= 10 ? 1 : 2) + " kWh"; }
  function lav(navn, attr, tekst) {
    var el = document.createElementNS(NS, navn);
    Object.keys(attr).forEach(function (k) { el.setAttribute(k, attr[k]); });
    if (tekst != null) el.textContent = tekst;
    return el;
  }

  document.querySelectorAll(".periode-graf").forEach(function (boks) {
    var kilde = boks.getAttribute("data-kilde");
    var H = +boks.getAttribute("data-h") || 170, W = 640, TOP = 14, BUND = 22, VENSTRE = 34;
    // tegn i skærmens egen bredde, så tal og etiketter ikke bliver bittesmå på mobilen
    function bredde() { return Math.max(300, Math.round(svgBoks.clientWidth) || 640); }
    var visning = boks.getAttribute("data-visning"), data = null, nr = 0;
    var svgBoks = boks.querySelector(".pg-svg"), tip = boks.querySelector(".tip");
    var $ = function (s) { return boks.querySelector(s); };

    function hent(v, dato) {
      var url = kilde + (kilde.indexOf("?") >= 0 ? "&" : "?") + "visning=" + v + (dato ? "&dato=" + dato : "");
      var mit = ++nr;
      boks.classList.add("henter");
      fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          if (mit !== nr) return;             // et nyere klik er på vej
          boks.classList.remove("henter");
          if (!j) { $("[data-titel]").textContent = "Kunne ikke hente data"; return; }
          data = j; visning = j.visning; tegn();
        })
        .catch(function () { boks.classList.remove("henter"); });
    }

    function tegn() {
      skjulTip();
      boks.querySelectorAll(".segment button").forEach(function (b) {
        b.classList.toggle("aktiv", b.getAttribute("data-v") === visning);
      });
      $("[data-titel]").textContent = data.titel;
      $("[data-sum]").textContent = "I alt " + kwh(data.total);
      $("[data-ekstra]").textContent = data.ekstra || "";
      $("[data-enhed]").textContent = data.enhed + " · tryk på en søjle for at se tallet";
      $('[data-gaa="forrige"]').disabled = !data.forrige;
      $('[data-gaa="naeste"]').disabled = !data.naeste;
      $('[data-gaa="nu"]').hidden = data.er_nu;

      W = sidstBredde = bredde();
      var p = data.punkter, n = Math.max(p.length, 1);
      // pæn skala (1-2-2,5-5-10) med et minimum, så en tom eller lille graf ikke ser forkert ud
      var mindst = { dag: 0.5, uge: 2, maaned: 2, vinter: 20 }[visning] || 1;
      var maks = paen(Math.max.apply(null, p.map(function (x) { return x.v; }).concat([mindst])));
      var halv = maks / 2, dec = halv % 1 === 0 ? 0 : (halv * 10) % 1 === 0 ? 1 : 2;
      var bw = (W - VENSTRE) / n, hoejde = H - TOP - BUND;
      var svg = lav("svg", { "class": "graf", viewBox: "0 0 " + W + " " + H, role: "img" });
      [0, 0.5, 1].forEach(function (f) {
        var y = TOP + hoejde * (1 - f);
        svg.appendChild(lav("line", { "class": "akse", x1: VENSTRE, x2: W, y1: y, y2: y }));
        svg.appendChild(lav("text", { x: VENSTRE - 4, y: y + 3, "text-anchor": "end" }, dk(maks * f, dec)));
      });
      if (!data.total)
        svg.appendChild(lav("text", { "class": "tom", x: VENSTRE + (W - VENSTRE) / 2, y: TOP + hoejde / 2, "text-anchor": "middle" },
          "Intet forbrug i perioden"));
      p.forEach(function (pt, i) {
        var bh = Math.max(hoejde * pt.v / maks, pt.v > 0 ? 1 : 0);
        svg.appendChild(lav("rect", { "class": "s" + (pt.nu ? " nu" : ""), "data-i": i, x: VENSTRE + i * bw + bw * 0.15,
          y: H - BUND - bh, width: bw * 0.7, height: bh, rx: 2 }));
        // usynligt felt i hele søjlens højde, så små søjler også er nemme at ramme med en finger
        svg.appendChild(lav("rect", { "class": "hit", "data-i": i, x: VENSTRE + i * bw, y: TOP, width: bw, height: hoejde + BUND }));
        if (i % data.hver === 0)
          svg.appendChild(lav("text", { x: VENSTRE + i * bw + bw / 2, y: H - 7, "text-anchor": "middle" }, pt.l));
      });
      svgBoks.innerHTML = "";
      svgBoks.appendChild(svg);
    }

    function visTip(i) {
      var pt = data.punkter[i], svg = svgBoks.querySelector("svg");
      boks.querySelectorAll("rect.s.valgt").forEach(function (r) { r.classList.remove("valgt"); });
      var bar = svg.querySelector('rect.s[data-i="' + i + '"]');
      bar.classList.add("valgt");
      tip.innerHTML = "";
      var t = document.createElement("strong"); t.textContent = pt.t; tip.appendChild(t);
      var v = document.createElement("div"); v.className = "tal-tip"; v.textContent = kwh(pt.v); tip.appendChild(v);
      if (pt.x) { var x = document.createElement("div"); x.textContent = pt.x; tip.appendChild(x); }
      if (pt.ned) {
        var b = document.createElement("button");
        b.type = "button"; b.className = "lille";
        b.textContent = pt.ned[0] === "dag" ? "Vis døgnet time for time ›" : "Vis måneden dag for dag ›";
        b.addEventListener("click", function (e) { e.stopPropagation(); hent(pt.ned[0], pt.ned[1]); });
        tip.appendChild(b);
      }
      tip.hidden = false;
      var bb = boks.getBoundingClientRect(), rb = bar.getBoundingClientRect(), sb = svg.getBoundingClientRect();
      var midt = rb.left + rb.width / 2 - bb.left;
      var top = Math.min(rb.top, sb.top + sb.height - BUND * sb.height / H) - bb.top;
      var halv = tip.offsetWidth / 2;
      var left = Math.max(halv + 2, Math.min(bb.width - halv - 2, midt));
      tip.style.left = left + "px";
      tip.style.top = top + "px";
      tip.style.setProperty("--pil", (midt - left + halv) + "px");
    }
    function skjulTip() {
      tip.hidden = true;
      boks.querySelectorAll("rect.s.valgt").forEach(function (r) { r.classList.remove("valgt"); });
    }

    svgBoks.addEventListener("click", function (e) {
      var i = e.target.getAttribute && e.target.getAttribute("data-i");
      if (i == null) { skjulTip(); return; }
      if (!tip.hidden && boks.querySelector('rect.s.valgt[data-i="' + i + '"]')) { skjulTip(); return; }
      visTip(+i);
    });
    document.addEventListener("click", function (e) { if (!boks.contains(e.target)) skjulTip(); });

    boks.querySelectorAll(".segment button").forEach(function (b) {
      b.addEventListener("click", function () {
        // behold den viste periode: fx fra en uge til den måned ugen ligger i
        hent(b.getAttribute("data-v"), data && !data.er_nu ? data.dato : "");
      });
    });
    function gaa(hvor) {
      if (hvor === "nu") return hent(visning, "");
      if (data && data[hvor]) hent(visning, data[hvor]);
    }
    boks.querySelectorAll("[data-gaa]").forEach(function (b) {
      b.addEventListener("click", function () { gaa(b.getAttribute("data-gaa")); });
    });

    // swipe til venstre = næste periode, til højre = forrige
    var sx = null, sy = null;
    svgBoks.addEventListener("touchstart", function (e) {
      sx = e.touches[0].clientX; sy = e.touches[0].clientY;
    }, { passive: true });
    svgBoks.addEventListener("touchend", function (e) {
      if (sx == null) return;
      var dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
      sx = null;
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) gaa(dx < 0 ? "naeste" : "forrige");
    }, { passive: true });

    var sidstBredde = 0;
    window.addEventListener("resize", function () {
      if (data && Math.abs(bredde() - sidstBredde) > 20) { sidstBredde = bredde(); tegn(); }
    });
    hent(visning, "");
  });

  // Til/Fra-mærkerne ved e-mail-valgene følger afkrydsningen
  document.querySelectorAll("label.valg input[type=checkbox]").forEach(function (c) {
    c.addEventListener("change", function () {
      c.parentNode.querySelector(".til-fra").textContent = c.checked ? "Til" : "Fra";
    });
  });
})();

// Faner mellem bådene på Min side (kun når man har flere målere) og automatisk gem af e-mail-valg.
(function () {
  var faner = document.querySelector(".baad-faner");
  if (faner) {
    var knapper = faner.querySelectorAll("[data-fane]");
    function vaelg(id) {
      knapper.forEach(function (b) {
        var valgt = b.getAttribute("data-fane") === id;
        b.classList.toggle("aktiv", valgt);
        b.setAttribute("aria-selected", valgt);
      });
      document.querySelectorAll("[data-fane-indhold]").forEach(function (k) {
        k.hidden = k.getAttribute("data-fane-indhold") !== id;
      });
      try { localStorage.setItem("elmaaler-fane", id); } catch (e) {}
      window.dispatchEvent(new Event("resize"));     // grafen i den viste fane tegnes i rigtig bredde
    }
    knapper.forEach(function (b) {
      b.addEventListener("click", function () { vaelg(b.getAttribute("data-fane")); });
    });
    var gemt = null;
    try { gemt = localStorage.getItem("elmaaler-fane"); } catch (e) {}
    var findes = gemt && faner.querySelector('[data-fane="' + gemt + '"]');
    faner.hidden = false;
    vaelg(findes ? gemt : knapper[0].getAttribute("data-fane"));
  }

  document.querySelectorAll("form[data-autogem]").forEach(function (form) {
    var knap = form.querySelector("[data-gem-knap]"), skilt = document.querySelector("[data-gemt]"), timer = null;
    if (knap) knap.hidden = true;
    form.addEventListener("change", function () {
      fetch(form.action, { method: "POST", body: new FormData(form), credentials: "same-origin",
                           headers: { "X-Autogem": "1" } })
        .then(function (r) {
          if (!r.ok) throw new Error();
          skilt.textContent = "Gemt ✓"; skilt.className = "gemt";
        })
        .catch(function () {
          skilt.textContent = "Kunne ikke gemme – prøv igen"; skilt.className = "gemt fejl";
          if (knap) knap.hidden = false;
        })
        .then(function () {
          skilt.hidden = false;
          clearTimeout(timer);
          timer = setTimeout(function () { skilt.hidden = true; }, 2500);
        });
    });
  });
})();
