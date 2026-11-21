"""Byg manualer og quickguides som PDF i elmaaler/app/hjaelp/ (fra *.html + billeder/).

Kør:  python byg.py            (alle)
      python byg.py manual-bruger     (kun én – navnet før .html)
"""
import base64
import os
import sys
import tempfile
import time

from cdp import Edge

HER = os.path.dirname(os.path.abspath(__file__))
# PDF'erne lægges i programmet, så de kan åbnes under "Hjælp" på websiden
UD_MAPPE = os.path.join(HER, "..", "..", "elmaaler", "app", "hjaelp")

# kilde (.html), PDF-navn, tekst i sidefoden
DOKUMENTER = [
    ("manual-bruger", "Manual-medlem.pdf", "Elmålere – manual for medlemmer"),
    ("manual-havnekontor", "Manual-havnekontor.pdf", "Elmålere – manual for havnekontoret"),
    ("manual-administration", "Manual-administration.pdf", "Elmålere – manual for administration"),
    ("quickguide-medlem", "Quickguide-medlem.pdf", "Quickguide for medlemmer"),
    ("quickguide-havnekontor", "Quickguide-havnekontor.pdf", "Quickguide for havnekontoret"),
]


def fod(tekst):
    return ('<div style="width:100%;font:7.5pt Segoe UI,Arial,sans-serif;color:#6b7785;padding:0 14mm;display:flex;'
            f'justify-content:space-between"><span>{tekst} · Aabenraa Sejl Club</span>'
            '<span>Side <span class="pageNumber"></span> af <span class="totalPages"></span></span></div>')


def main(kun=None):
    e = Edge(os.path.join(tempfile.gettempdir(), "elmaaler-manual-edge"))
    try:
        for kilde, pdf, tekst in DOKUMENTER:
            if kun and kilde not in kun:
                continue
            e.gaa("file:///" + os.path.join(HER, kilde + ".html").replace("\\", "/"), vent=2)
            time.sleep(1)
            r = e.kald("Page.printToPDF", paperWidth=8.27, paperHeight=11.69, printBackground=True,
                       preferCSSPageSize=True, displayHeaderFooter=True, headerTemplate="<span></span>",
                       footerTemplate=fod(tekst))
            ud = os.path.normpath(os.path.join(UD_MAPPE, pdf))
            with open(ud, "wb") as f:
                f.write(base64.b64decode(r["data"]))
            print("Gemt:", ud, os.path.getsize(ud) // 1024, "kB")
    finally:
        e.luk()


if __name__ == "__main__":
    main(sys.argv[1:] or None)
