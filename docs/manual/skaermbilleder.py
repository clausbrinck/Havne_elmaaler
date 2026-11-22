"""Tag manualens skærmbilleder med nummererede markeringer.

Kræver et kørende testsystem på http://localhost:8000 med testdata (se laes-mig.md).
Kør:  python skaermbilleder.py   → billeder i billeder/
"""
import json
import os
import sys
import tempfile
import time

from cdp import Edge

B = "http://localhost:8000"
MAPPE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "billeder")
KODE = "testtest1"
BRUGERE = {"hans": "hans@eksempel.dk", "kontor": "kontor@aabenraa-sejlclub.dk", "admin": "admin@aabenraa-sejlclub.dk"}
MOBIL, PC = (390, 844, True), (1280, 860, False)
# testsystemets lokale mappe vises som serverens rigtige sti
STI = "document.querySelectorAll('code').forEach(function(c){ if (c.textContent.indexOf('manualdata') >= 0) c.textContent = '/data/backup'; });"
# vælg fanen Havørnen (siden husker ellers den sidst valgte båd)
HAVOERN = ("var f=__find('.baad-faner button','Havørnen'); if (f) f.click();"
           "await new Promise(r=>setTimeout(r,700)); window.scrollTo(0,0);")

MARKER_JS = r"""
window.__find = function (sel, tekst) {
  var els = Array.prototype.slice.call(document.querySelectorAll(sel)).filter(function (e) {
    return e.getClientRects().length && !e.closest('[hidden]'); });
  if (tekst) els = els.filter(function (e) { return (e.textContent || '').indexOf(tekst) >= 0 || (e.value || '').indexOf(tekst) >= 0
                                                  || (e.placeholder || '').indexOf(tekst) >= 0; });
  return els[0];
};
window.__mk = function (liste) {
  document.querySelectorAll('.__mk').forEach(function (e) { e.remove(); });
  var fejl = [];
  liste.forEach(function (m) {
    var el = __find(m[0], m[1]);
    if (!el) { fejl.push(m[0] + ' / ' + (m[1] || '')); return; }
    var r = el.getBoundingClientRect(), x = r.left + scrollX, y = r.top + scrollY;
    var b = document.createElement('div'); b.className = '__mk';
    Object.assign(b.style, {position: 'absolute', left: (x - 4) + 'px', top: (y - 4) + 'px', width: (r.width + 8) + 'px',
      height: (r.height + 8) + 'px', border: '3px solid #e4572e', borderRadius: '8px', zIndex: 9998,
      pointerEvents: 'none', boxShadow: '0 0 0 2px rgba(255,255,255,.8)'});
    var n = document.createElement('div'); n.className = '__mk'; n.textContent = m[2];
    var nx = m[3] === 'h' ? x + r.width - 10 : Math.max(x - 15, 2);
    Object.assign(n.style, {position: 'absolute', left: nx + 'px', top: Math.max(y - 15, 2) + 'px', width: '26px',
      height: '26px', borderRadius: '50%', background: '#e4572e', color: '#fff', font: '700 15px/26px Segoe UI, sans-serif',
      textAlign: 'center', zIndex: 9999, boxShadow: '0 1px 4px rgba(0,0,0,.35)'});
    document.body.appendChild(b); document.body.appendChild(n);
  });
  return fejl;
};
window.__klip = function (liste, pad) {
  var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, fejl = [];
  liste.forEach(function (m) {
    var el = __find(m[0], m[1]);
    if (!el) { fejl.push(m[0] + ' / ' + (m[1] || '')); return; }
    var r = el.getBoundingClientRect();
    x0 = Math.min(x0, r.left + scrollX); y0 = Math.min(y0, r.top + scrollY);
    x1 = Math.max(x1, r.right + scrollX); y1 = Math.max(y1, r.bottom + scrollY);
  });
  var w = document.documentElement.clientWidth;
  return {x: Math.max(x0 - pad, 0), y: Math.max(y0 - pad, 0), width: Math.min(x1 + pad, w) - Math.max(x0 - pad, 0),
          height: Math.min(y1 - y0 + 2 * pad, 1150), fejl: fejl};
};
"""

# hvert billede: navn, bruger, url, (bredde, højde, mobil), forbered-JS, klip-liste (None = skærmen), markeringer
# markering: [css-selektor, tekst som elementet skal indeholde (eller ""), nummer, ("h" = nummer til højre)]
BILLEDER = [
    # ------------------------------------------------------------ fælles
    ("login", None, "/login", MOBIL, "", [["header.top", ""], [".login-boks", ""]],
     [["#email", "", 1], ["#kode", "", 2], ["button", "Log ind", 3], ["a", "Glemt adgangskode", 4]]),
    ("glemt", None, "/glemt", MOBIL, "", [["header.top", ""], [".kort", ""]], [["input[name=email]", "", 1], ["button", "Send link", 2]]),
    # ------------------------------------------------------------ medlem
    ("minside-enkel", "hans", "/min-side", MOBIL, "", None,
     [[".enkel-kort .badge", "", 1, "h"], [".enkel-kort .besked", "", 2], [".enkel-kort .enkel-tal", "", 3],
      [".enkel-kort .bar", "", 4], [".visning-skift button", "", 5, "h"]]),
    ("minside-enkel-moerk", "hans", "/min-side", MOBIL, "", None, []),
    ("minside-top", "hans", "/min-side", MOBIL, HAVOERN, None,
     [[".samlet", "", 1], [".baad-faner", "", 2], [".maalerkort .maaler-hoved .badge", "", 3, "h"],
      [".maalerkort .tal.effekt", "", 4]]),
    ("minside-gruppe", "hans", "/min-side", MOBIL, HAVOERN,
     [[".maalerkort .gitter", ""], [".maalerkort .gruppe-boks", ""]],
     [[".maalerkort .tal.effekt .amp", "", 1], [".maalerkort .tal.effekt .forklaring", "", 2],
      [".maalerkort .gruppe-boks .pris", "", 3, "h"], [".maalerkort .gruppe-boks .bar", "", 4],
      [".maalerkort .gruppe-boks div.muted", "", 5]]),
    ("minside-graf", "hans", "/min-side", MOBIL, HAVOERN,
     [[".maalerkort .periode-graf", ""]],
     [[".maalerkort .segment", "", 1], [".maalerkort [data-gaa=forrige]", "", 2], [".maalerkort .pg-titel", "", 3],
      [".maalerkort [data-gaa=naeste]", "", 4, "h"], [".maalerkort svg rect.s.nu", "", 5, "h"]]),
    ("minside-graf-doegn", "hans", "/min-side", MOBIL, HAVOERN +
     "__find('.maalerkort .segment button','Døgn').click(); await new Promise(r=>setTimeout(r,900));"
     "__find('.maalerkort [data-gaa=forrige]').click(); await new Promise(r=>setTimeout(r,900));"
     "var h=__find('.maalerkort rect.hit[data-i=\"19\"]'); h.dispatchEvent(new MouseEvent('click',{bubbles:true}));",
     [[".maalerkort .periode-graf", ""]],
     [[".maalerkort .segment button", "Døgn", 1], [".maalerkort .tip", "", 2, "h"],
      [".maalerkort .pg-fod button", "Gå til nu", 3]]),
    ("minside-offline", "hans", "/min-side", MOBIL,
     "__find('.baad-faner button','Marlin').click(); await new Promise(r=>setTimeout(r,600));",
     [[".baad-faner", ""], [".maalerkort .besked", ""]],
     [[".baad-faner button", "Marlin", 1], [".maalerkort .maaler-hoved .badge", "", 2, "h"],
      [".maalerkort .besked.alarm", "", 3]]),
    ("minside-email", "hans", "/min-side", MOBIL,
     "__find('.baad-faner button','Havørnen').click(); var c=__find('input[name=mail_dag]'); c.click();"
     "await new Promise(r=>setTimeout(r,800));",
     [["#e-mail", ""]],
     [["#e-mail .mailadresse", "", 1], ["#e-mail label.valg", "Daglig", 2], ["#e-mail .gemt", "", 3, "h"],
      ["#e-mail select[name=periode]", "", 4], ["#e-mail button", "Send rapport", 5]]),
    ("minside-grupper", "hans", "/min-side", MOBIL, "", [[".kort", "Forbrugsgrupper (november"]],
     [["tr.min-gruppe", "Havørnen", 1], ["tr.min-gruppe", "Marlin", 2]]),
    ("minside-historik", "hans", "/min-side", MOBIL, "", [[".kort", "Tidligere måneder"]], []),
    ("konto", "hans", "/min-side/konto", MOBIL, "", None,
     [["nav a", "Konto Administration", 1], [".mailadresse", "", 2], [".kort", "Skift e-mail", 3]]),
    ("konto-kode", "hans", "/min-side/konto", MOBIL, "", [[".kort", "Skift adgangskode"]], []),
    ("hjaelp", "hans", "/hjaelp", MOBIL, "", [["header.top", ""], [".hjaelp-liste", ""]],
     [["nav a", "Hjælp", 1], ["a.hjaelp", "Quickguide", 2]]),
    # ------------------------------------------------------------ havnekontor
    ("kontor-overblik", "kontor", "/kontor", PC, "await new Promise(r=>setTimeout(r,800));", None,
     [["header nav", "", 1], [".gitter", "Samlet effekt", 2], [".periode-graf", "", 3]]),
    ("kontor-maalere", "kontor", "/kontor", PC, "", [[".kort .maaler-hoved", "Målere"], ["#maalertabel tbody tr:nth-child(12)", ""]],
     [["input[data-filter]", "", 1], ["table a", "", 2]]),
    ("kontor-maaler", "kontor", "/kontor/maaler/12", PC, "", [[".kort", "Tildeling"]],
     [["select[name=bruger_id]", "", 1], ["input[name=email]", "", 2], ["input[name=start_dato]", "", 3],
      ["button", "Tildel måler", 4], ["button", "Fjern fra bruger", 5], ["input[name=baad]", "", 6]]),
    ("kontor-brugere", "kontor", "/kontor/brugere", PC, "", None,
     [["a.knap", "Importér fra CSV", 1], ["a.knap", "Målere der ikke er i brug", 2], ["button", "Opret bruger", 3],
      ["input[data-filter]", "", 4]]),
    ("kontor-bruger", "kontor", "__hans__", PC, "", None,
     [[".kort", "Oplysninger", 1], [".kort", "Adgang", 2], ["button", "Fjern", 3], ["button", "Tildel", 4]]),
    ("kontor-ubrugte", "kontor", "/kontor/ubrugte?dage=14&kwh=40", PC, "", None,
     [["input[name=dage]", "", 1], ["input[name=kwh]", "", 2], ["label.check", "Vælg alle", 3],
      ["button", "Fjern valgte", 4]]),
    ("kontor-import", "kontor", "/kontor/import", PC, "", None,
     [["pre", "", 1], ["input[type=file]", "", 2], ["label.check", "invitation", 3]]),
    ("kontor-opgoerelse", "admin", "/kontor/opgoerelse?maaned=2026-10", PC, "", None,
     [["select", "", 1], ["a.knap", "Hent CSV", 2], ["a.knap", "Hele vinterperioden", 3], ["button", "Genberegn", 4],
      ["input[data-filter]", "", 5]]),
    ("kontor-opgoerelse-nov", "admin", "/kontor/opgoerelse?maaned=2026-11", PC, "",
     [[".kort", "Foreløbig"], [".kort", "Fordeling på forbrugsgrupper"]],
     [["a.knap", "Månedsrapport (PDF)", 1], [".kort", "Fordeling på forbrugsgrupper", 2]]),
    ("kontor-konto", "kontor", "/min-side/konto", PC, "", [[".kort", "Mine havnekontor-e-mails"]], []),
    # ------------------------------------------------------------ systemadministrator
    ("system-status", "admin", "/system", PC, "", None,
     [["header nav a", "System", 1], ["nav.faner", "", 2], [".kort", "Zigbee-målere", 3]]),
    ("system-log", "admin", "/system/log", PC, "", None, [["select[name=type]", "", 1], ["input[name=q]", "", 2]]),
    ("system-maalere", "admin", "/system/maalere", PC, "", None,
     [["input[data-filter]", "", 1], ["button", "Gem ændringer", 2, "h"]]),
    ("system-database", "admin", "/system/database", PC, STI, [[".kort h2", "Backup"], [".kort p.muted", "Gendannelse"]],
     [[".kort .besked", "", 1], ["button", "Lav backup nu", 2], ["table a", "", 3]]),
    # kræver testsystemet startet med BACKUP_RCLONE_REMOTE=krypt: og en "Backup kopieret til"-linje i loggen
    ("system-database-sky", "admin", "/system/database", PC, STI, [[".kort h2", "Backup"], [".kort .besked", ""], ["button", "Kopiér til skyen nu"]],
     [[".kort .besked", "", 1], ["button", "Kopiér til skyen nu", 2, "h"]]),
    ("system-database-roed", "admin", "/system/database", PC, STI, [[".kort h2", "Backup"], [".kort .besked", ""]],
     [[".kort .besked", "", 1]]),
    ("system-database-nulstil", "admin", "/system/database", PC, "", [[".kort", "Nulstil fejlmålt forbrug"]], []),
    ("system-indstillinger", "admin", "/system/indstillinger", PC, "", [[".kort", "Mails – fælles opsætning"]],
     [["input[name=ekstra_kontor_emails]", "", 1], ["input[name=bestyrelse_emails]", "", 2],
      ["input[name=mail_time]", "", 3]]),
    ("system-grupper", "admin", "/system/indstillinger", PC, "", [[".kort", "Forbrugsgrupper og afregningsmåneder"]],
     [["button", "Gem forbrugsgrupper", 1]]),
    ("system-debug", "admin", "/system/debug", PC, "", None, [[".kort", "Tilføj simuleret forbrug", 1]]),
]


def main(kun=None):
    os.makedirs(MAPPE, exist_ok=True)
    e = Edge(os.path.join(tempfile.gettempdir(), "elmaaler-manual-edge"))
    logget_ind = "?"          # ukendt: ryd altid cookies før første billede
    try:
        for navn, bruger, url, (w, h, mobil), forbered, klip, mk in BILLEDER:
            if kun and navn not in kun:
                continue
            e.størrelse(w, h, mobil)
            # billeder der slutter på "-moerk" tages med telefonens mørke tilstand
            e.kald("Emulation.setEmulatedMedia",
                   features=[{"name": "prefers-color-scheme", "value": "dark" if navn.endswith("-moerk") else "light"}])
            if bruger != logget_ind:
                e.kald("Network.clearBrowserCookies")
                if bruger:
                    e.gaa(B + "/login")
                    e.js(f"document.getElementById('email').value={json.dumps(BRUGERE[bruger])};"
                         f"document.getElementById('kode').value={json.dumps(KODE)};"
                         "document.querySelector('form').submit();")
                    time.sleep(1.5)
                logget_ind = bruger
            if url == "__hans__":
                e.gaa(B + "/kontor/brugere")
                url = e.js("Array.from(document.querySelectorAll('a')).find(a=>a.textContent.includes('Hans Hansen'))"
                           ".getAttribute('href')")
            e.gaa(B + url, vent=1.8)
            if url == "/min-side":
                # Min side: "minside-enkel..." i enkel visning, øvrige "minside-..." i avanceret visning
                oensket = "enkel" if navn.startswith("minside-enkel") else "avanceret"
                knap = e.js("(document.querySelector('.visning-skift button')||{}).value")
                nu_vist = "enkel" if knap == "avanceret" else "avanceret"
                if nu_vist != oensket:
                    e.js("document.querySelector('.visning-skift button').click()")
                    time.sleep(1.8)
            e.js(MARKER_JS)
            if forbered:
                e.js(f"(async () => {{ {forbered} }})()")
                time.sleep(0.4)
            fejl = e.js(f"__mk({json.dumps(mk)})")
            fil = os.path.join(MAPPE, navn + ".png")
            if klip:
                k = e.js(f"__klip({json.dumps(klip)}, 18)")
                if k["fejl"] or k["width"] <= 0:
                    print("SPRUNGET OVER – fandt ikke udsnittet:", k["fejl"], navn)
                    continue
                k.pop("fejl")
                r = e.kald("Page.captureScreenshot", format="png", captureBeyondViewport=True, clip={**k, "scale": 1})
                import base64
                with open(fil, "wb") as f:
                    f.write(base64.b64decode(r["data"]))
            else:
                e.js("window.scrollTo(0,0)")
                e.billede(fil, hel_side=False)
            print(("FEJL " + str(fejl) + " ") if fejl else "ok   ", navn)
    finally:
        e.luk()


if __name__ == "__main__":
    main(sys.argv[1:] or None)
