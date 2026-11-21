"""Månedsrapport til bestyrelsen som PDF (reportlab).

Sendes automatisk den 1. kl. mail_tidspunkt for den netop afsluttede afregningsmåned (nov–mar),
efter at måneden er låst. Kan også hentes/sendes manuelt fra Opgørelse-siden.
Layoutet følger eksemplerne i docs/eksempler/maanedsrapport-*.pdf.
"""
import io
import os
from xml.sax.saxutils import escape

from . import config, db, forbrug
from .rapport import dk
from .tid import (fmt_tid, forrige_maaned, lokal, maaned_grænser, maaned_navn,
                  naeste_maaned, nu)

LOGO = os.path.join(os.path.dirname(__file__), "static", "logo.png")
MND_KORT = ["Jan", "Feb", "Mar", "Apr", "Maj", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dec"]


def modtagere():
    return [e.strip() for e in (db.hent("bestyrelse_emails") or "").replace(";", ",").split(",") if e.strip()]


# ---------------------------------------------------------------- tal

def _saeson(maaned):
    """Afregningsmånederne i den vintersæson, maaned hører til, i rækkefølge (fx 2026-11 … 2027-03)."""
    aar, m = int(maaned[:4]), int(maaned[5:7])
    start = aar if m >= 7 else aar - 1
    ud, x = [], f"{start}-07"
    for _ in range(12):
        if forbrug.afregnes(x):
            ud.append(x)
        x = naeste_maaned(x)
    return start, ud


def data(maaned):
    """Alle tal til rapporten for maaned ('ÅÅÅÅ-MM')."""
    fra, til, d0, d1 = maaned_grænser(maaned)
    rows, laast = forbrug.maanedsopgoerelse(maaned)
    afregnet = [r for r in rows if r["bruger_id"] is not None and r["afregnes"]]
    fordeling = forbrug.gruppe_fordeling(afregnet)

    with db.db() as c:
        total = sum(forbrug.dagsserie(c, None, d0, d1).values())
        dage = list(forbrug.dagsserie(c, None, d0, d1).values())
        fm = forrige_maaned(maaned)
        f0, f1 = maaned_grænser(fm)[2:]
        forrige_total = sum(forbrug.dagsserie(c, None, f0, f1).values())
        foerste = c.execute("SELECT MIN(ts) FROM aflaesninger").fetchone()[0]
        sa = f"{int(maaned[:4]) - 1}{maaned[4:]}"
        sa0, sa1 = maaned_grænser(sa)[2:]
        sidste_aar = (sum(forbrug.dagsserie(c, None, sa0, sa1).values())
                      if foerste and foerste <= maaned_grænser(sa)[0] else None)

        # drift
        st = c.execute("SELECT SUM(s.tilgaengelig=1) online, COUNT(*) alle FROM status s JOIN maalere m "
                       "ON m.id=s.maaler_id WHERE m.aktiv=1 AND s.sidst_set IS NOT NULL").fetchone()
        slots = (til - fra) // (config.STORE_MINUTES * 60)
        afl = c.execute("SELECT COUNT(*) n, COUNT(DISTINCT maaler_id) m FROM aflaesninger WHERE ts>? AND ts<=?",
                        (fra, til)).fetchone()
        dataprocent = min(afl["n"] / (afl["m"] * slots) * 100, 100) if afl["m"] and slots else None
        offline = sum(len(r["tekst"].split(":", 1)[1].split(",")) for r in c.execute(
            "SELECT tekst FROM haendelser WHERE type='måler' AND tekst LIKE 'Offline >%' AND ts>=? AND ts<?",
            (fra, til)))
        ha_udfald = c.execute("SELECT COUNT(*) FROM haendelser WHERE type='fejl' AND tekst LIKE "
                              "'Opsamling fra Home Assistant fejlede%' AND ts>=? AND ts<?", (fra, til)).fetchone()[0]
        backup_dage = len({lokal(r[0]).date() for r in c.execute(
            "SELECT ts FROM haendelser WHERE type='backup' AND ts>=? AND ts<?", (fra, til))})
        nye = c.execute("SELECT COUNT(*) FROM tildelinger WHERE start_ts>=? AND start_ts<?", (fra, til)).fetchone()[0]
        afsluttet = c.execute("SELECT COUNT(*) FROM tildelinger WHERE slut_ts>=? AND slut_ts<?",
                              (fra, til)).fetchone()[0]
        aktive = c.execute("SELECT maaler_id FROM tildelinger WHERE start_ts<? AND (slut_ts IS NULL OR slut_ts>=?)",
                           (til, til)).fetchall()
        ubrugte = sum(1 for t in aktive if forbrug.sum_delta(c, t["maaler_id"], til - 30 * 86400, til) < 0.5)
        maalere = c.execute("SELECT COUNT(*) FROM maalere WHERE aktiv=1").fetchone()[0]

    # vintersæsonen indtil nu
    start_aar, saeson = _saeson(maaned)
    vinter = []
    for m in saeson:
        if m > maaned:
            vinter.append((m, None, None))
            continue
        r_m = rows if m == maaned else forbrug.maanedsopgoerelse(m)[0]
        a_m = [r for r in r_m if r["bruger_id"] is not None and r["afregnes"]]
        vinter.append((m, sum(r["kwh"] for r in r_m), sum(r["pris"] or 0 for r in a_m)))

    kr = sum(r["pris"] or 0 for r in afregnet)
    nr = saeson.index(maaned) + 1 if maaned in saeson else 0
    aend = (total - forrige_total) / forrige_total * 100 if forrige_total else None
    if nr == 1:
        ingress = f"Første afregningsmåned i vintersæsonen {start_aar}/{(start_aar + 1) % 100:02d}."
    elif nr == len(saeson):
        ingress = (f"Sidste afregningsmåned i vintersæsonen {start_aar}/{(start_aar + 1) % 100:02d}. "
                   "Sæsonens samlede opkrævning kan nu sendes ud til medlemmerne inden standerhejsning.")
    else:
        ingress = f"Afregningsmåned {nr} af {len(saeson)} i vintersæsonen {start_aar}/{(start_aar + 1) % 100:02d}."
    if aend is not None:
        ingress += (f" Forbruget er {'steget' if aend >= 0 else 'faldet'} {dk(abs(aend), 0)} % "
                    f"i forhold til {maaned_navn(fm).split()[0]}.")

    uden = [r for r in rows if r["bruger_id"] is None and r["kwh"] >= 0.5]
    top = sorted(afregnet, key=lambda r: -r["kwh"])[:10]
    return {
        "maaned": maaned, "titel": maaned_navn(maaned).capitalize(), "laast": laast,
        "laast_ts": forbrug.er_laast(maaned), "genereret": nu(), "ingress": ingress,
        "total": total, "forrige_navn": maaned_navn(fm).split()[0], "aend": aend, "sidste_aar": sidste_aar,
        "kr": kr, "baade": len(afregnet), "maalere": maalere,
        "grupper": [(nr_, g) for nr_, g in fordeling.items()],
        "dage": dage, "vinter": vinter, "saeson": f"{start_aar}/{(start_aar + 1) % 100:02d}",
        "sidste_i_saeson": nr == len(saeson), "top": top,
        "top_andel": sum(r["kwh"] for r in top) / total * 100 if total else 0,
        "drift": {"online": st["online"] or 0, "alle": st["alle"] or 0, "data": dataprocent, "offline": offline,
                  "ha": ha_udfald, "backup": backup_dage, "dage_i_maaned": (d1 - d0).days,
                  "nye": nye, "afsluttet": afsluttet, "ubrugte": ubrugte, "ledige": maalere - len(aktive)},
        "uden_bruger": (len(uden), sum(r["kwh"] for r in uden)),
    }


# ---------------------------------------------------------------- PDF

def pdf(d):
    """Byg PDF'en og returnér den som bytes."""
    from PIL import Image
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, KeepTogether, PageBreak, PageTemplate,
                                    Paragraph, Spacer, Table, TableStyle)

    HAV, HAV2, BLAA = colors.HexColor("#0b3a5b"), colors.HexColor("#13557f"), colors.HexColor("#3f8fc4")
    GRAA, LINJE, TEKST = colors.HexColor("#6b7785"), colors.HexColor("#dde3e9"), colors.HexColor("#1d2733")
    ORANGE, ROED, GROEN = colors.HexColor("#c77d00"), colors.HexColor("#b0541a"), colors.HexColor("#1f8a4c")
    GRUPPE = {1: ("#e3f4ea", "#17663a"), 2: ("#e7f0f9", "#1c4f7d"), 3: ("#fff4d6", "#8a5a00"),
              4: ("#ffe6cc", "#994800"), 5: ("#fbe0dc", "#9a2a1c"), 6: ("#f2d7ef", "#7a1f6f")}
    B, V, H = A4[0], 15 * mm, A4[1]
    BREDDE = B - 2 * V

    logo = Image.open(LOGO).convert("RGBA")
    svag = logo.copy()
    svag.putalpha(svag.getchannel("A").point(lambda a: int(a * 0.07)))
    logo_r, svag_r = ImageReader(logo), ImageReader(svag)

    def st(navn, **kw):
        base = dict(fontName="Helvetica", fontSize=9, leading=12, textColor=TEKST)
        base.update(kw)
        return ParagraphStyle(navn, **base)
    S = {
        "tekst": st("tekst", fontSize=10, leading=14),
        "h2": st("h2", fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=HAV, spaceBefore=10, spaceAfter=4),
        "lille": st("lille", fontSize=8, leading=10, textColor=GRAA),
        "celle": st("celle", fontSize=8.5, leading=10.5),
        "celle_h": st("celle_h", fontSize=8.5, leading=10.5, alignment=TA_RIGHT),
        "kpi": st("kpi", fontName="Helvetica-Bold", fontSize=15, leading=18, textColor=HAV),
        "kpi_l": st("kpi_l", fontSize=7.5, leading=9.5, textColor=GRAA),
        "boks": st("boks", fontSize=8.5, leading=11),
    }

    def side(canv, doc):
        canv.saveState()
        canv.drawImage(svag_r, (B - 125 * mm) / 2, (H - 125 * mm) / 2, 125 * mm, 125 * mm, mask="auto")
        top = H - 14 * mm
        canv.drawImage(logo_r, V, top - 13 * mm, 13 * mm, 13 * mm, mask="auto")
        canv.setFillColor(HAV)
        canv.setFont("Helvetica-Bold", 13)
        canv.drawString(V + 16 * mm, top - 6 * mm, config.KLUB_NAVN)
        canv.setFillColor(GRAA)
        canv.setFont("Helvetica", 7.5)
        canv.drawString(V + 16 * mm, top - 10.5 * mm, "ELMÅLERE · MÅNEDSRAPPORT TIL BESTYRELSEN")
        canv.setFillColor(HAV)
        canv.setFont("Helvetica-Bold", 16)
        canv.drawRightString(B - V, top - 6.5 * mm, d["titel"])
        canv.setFillColor(GRAA)
        canv.setFont("Helvetica", 7.5)
        status = (f"Låst {fmt_tid(d['laast_ts'])}" if d["laast"] and d["laast_ts"] else "Foreløbige tal (ikke låst)")
        canv.drawRightString(B - V, top - 10.5 * mm, status if doc.page == 1 else f"side {doc.page}")
        canv.setStrokeColor(HAV)
        canv.setLineWidth(1.6)
        canv.line(V, top - 14.5 * mm, B - V, top - 14.5 * mm)
        canv.setStrokeColor(LINJE)
        canv.setLineWidth(0.6)
        canv.line(V, 12 * mm, B - V, 12 * mm)
        canv.setFont("Helvetica", 7)
        canv.drawString(V, 8.5 * mm, f"Genereret automatisk af elmåler-systemet {fmt_tid(d['genereret'])}")
        canv.drawRightString(B - V, 8.5 * mm, f"Side {doc.page}")
        canv.restoreState()

    class Andel(Flowable):
        """Lille vandret bjælke med procenttal – til gruppetabellen."""
        def __init__(self, frac, tekst, bredde):
            super().__init__()
            self.frac, self.tekst, self.bredde = frac, tekst, bredde

        def wrap(self, aw, ah):
            return self.bredde, 9

        def draw(self):
            lang = max((self.bredde - 12 * mm) * self.frac, 1.5)
            self.canv.setFillColor(BLAA)
            self.canv.roundRect(0, 2, lang, 5, 2.5, stroke=0, fill=1)
            self.canv.setFillColor(GRAA)
            self.canv.setFont("Helvetica", 7)
            self.canv.drawString(lang + 3, 2.3, self.tekst)

    def soejler(vaerdier, etiketter, bredde, hoejde, hver, enhed):
        top, bund, v0 = 12, 12, 30
        mx = max(vaerdier + [1])
        e = 10 ** (len(str(int(mx))) - 1)
        mx = next(s * e for s in (1, 2, 2.5, 5, 10) if s * e >= mx)
        dr = Drawing(bredde, hoejde)
        ph = hoejde - top - bund
        for f in (0, 0.5, 1):
            y = bund + ph * f
            dr.add(Line(v0, y, bredde, y, strokeColor=LINJE, strokeWidth=0.6))
            dr.add(String(v0 - 3, y - 2.5, dk(mx * f, 0), fontName="Helvetica", fontSize=6.5,
                          fillColor=GRAA, textAnchor="end"))
        bw = (bredde - v0) / max(len(vaerdier), 1)
        for i, v in enumerate(vaerdier):
            dr.add(Rect(v0 + i * bw + bw * 0.15, bund, bw * 0.7, ph * v / mx, fillColor=BLAA, strokeColor=None,
                        rx=1, ry=1))
            if i % hver == 0:
                dr.add(String(v0 + i * bw + bw / 2, 2, etiketter[i], fontName="Helvetica", fontSize=6.5,
                              fillColor=GRAA, textAnchor="middle"))
        if vaerdier:
            gns = sum(vaerdier) / len(vaerdier)
            y = bund + ph * gns / mx
            dr.add(Line(v0, y, bredde, y, strokeColor=ORANGE, strokeWidth=0.8, strokeDashArray=[3, 2]))
            dr.add(String(bredde, hoejde - 8, f"- - -  gennemsnit {dk(gns, 0)} {enhed}", fontName="Helvetica",
                          fontSize=6.5, fillColor=ORANGE, textAnchor="end"))
        return dr

    def tabel(data, bredder, hoejre=(), sum_raekke=False, hoved=True):
        t = Table(data, colWidths=bredder, repeatRows=1 if hoved else 0)
        stil = [("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
                ("TEXTCOLOR", (0, 0), (-1, -1), TEKST),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINJE)]
        if hoved:
            stil += [("FONT", (0, 0), (-1, 0), "Helvetica", 6.8), ("TEXTCOLOR", (0, 0), (-1, 0), GRAA),
                     ("LINEBELOW", (0, 0), (-1, 0), 1.2, HAV)]
        for k in hoejre:
            stil.append(("ALIGN", (k, 0), (k, -1), "RIGHT"))
        if sum_raekke:
            stil += [("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8.5), ("LINEABOVE", (0, -1), (-1, -1), 1.2, HAV),
                     ("LINEBELOW", (0, -1), (-1, -1), 0, colors.white)]
        t.setStyle(TableStyle(stil))
        return t

    def boks(tekst, farve, baggrund):
        t = Table([[Paragraph(tekst, S["boks"])]], colWidths=[BREDDE])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(baggrund)),
                               ("LINEBEFORE", (0, 0), (0, -1), 3.5, colors.HexColor(farve)),
                               ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        return [t, Spacer(1, 5)]

    def gruppe_celle(nr, label):
        lys, moerk = GRUPPE.get(nr, ("#eeeeee", "#333333"))
        return Paragraph(f'<font color="{moerk}"><b>{label}</b></font>', S["celle"]), colors.HexColor(lys)

    fl = []
    fl.append(Paragraph(d["ingress"], S["tekst"]))
    fl.append(Spacer(1, 8))

    # nøgletal
    if d["aend"] is None:
        aend = "Samlet forbrug, alle målere"
    else:
        farve = "#b0541a" if d["aend"] >= 0 else "#1f8a4c"
        aend = (f'Samlet forbrug · <font color="{farve}">{"op" if d["aend"] >= 0 else "ned"} '
                f'{dk(abs(d["aend"]), 0)} %</font> mod {d["forrige_navn"]}')
    if d["sidste_aar"]:
        aend += f" · samme måned sidste år: {dk(d['sidste_aar'], 0)} kWh"
    kpi = [(f'{dk(d["total"], 0)} <font size="9" color="#6b7785">kWh</font>', aend),
           (f'{dk(d["kr"], 0)} <font size="9" color="#6b7785">kr</font>', "Til opkrævning for måneden"),
           (f'{d["baade"]} <font size="9" color="#6b7785">både</font>', f'Afregnet · {d["maalere"]} målere i alt'),
           (f'{dk(d["total"] / d["baade"] if d["baade"] else 0, 1)} <font size="9" color="#6b7785">kWh</font>',
            f'Gennemsnit pr. båd · {dk(d["kr"] / d["baade"] if d["baade"] else 0, 0)} kr')]
    kt = Table([[[Paragraph(a, S["kpi"]), Paragraph(b, S["kpi_l"])] for a, b in kpi]],
               colWidths=[BREDDE / 4] * 4)
    kt.setStyle(TableStyle([("BOX", (i, 0), (i, 0), 0.6, LINJE) for i in range(4)] +
                           [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 7),
                            ("BACKGROUND", (0, 0), (-1, -1), colors.Color(1, 1, 1, 0.85))]))
    fl.append(kt)

    # forbrugsgrupper
    fl.append(Paragraph("Forbrugsgrupper", S["h2"]))
    antal = sum(g["antal"] for _, g in d["grupper"]) or 1
    maks = max([g["antal"] for _, g in d["grupper"]] + [1])
    bw_andel = 50 * mm
    rk = [["GRUPPE", "PRIS PR. BÅD", "ANTAL BÅDE", "ANDEL", "KWH", "BELØB"]]
    baggrunde = []
    for i, (nr, g) in enumerate(d["grupper"], start=1):
        celle, lys = gruppe_celle(nr, g["label"])
        baggrunde.append(("BACKGROUND", (0, i), (0, i), lys))
        rk.append([celle, f'{dk(g["pris"], 0)} kr', str(g["antal"]),
                   Andel(g["antal"] / maks, f'{dk(g["antal"] / antal * 100, 0)} %', bw_andel),
                   dk(g["kwh"], 0), f'{dk(g["beloeb"], 0)} kr'])
    rk.append(["I alt", "", str(sum(g["antal"] for _, g in d["grupper"])), "",
               dk(sum(g["kwh"] for _, g in d["grupper"]), 0), f'{dk(d["kr"], 0)} kr'])
    gt = tabel(rk, [34 * mm, 24 * mm, 22 * mm, bw_andel + 4, 20 * mm, BREDDE - 100 * mm - bw_andel - 4],
               hoejre=(1, 2, 4, 5), sum_raekke=True)
    gt.setStyle(TableStyle(baggrunde))
    fl.append(gt)

    # pr. dag
    fl.append(Paragraph("Samlet forbrug pr. dag – alle målere (kWh)", S["h2"]))
    fl.append(soejler(d["dage"], [str(i + 1) for i in range(len(d["dage"]))], BREDDE, 120, 2, "kWh/dag"))

    # vintersæsonen
    vrk = [["MÅNED", "KWH", "OPKRÆVNING"]]
    for m, kwh, kr in d["vinter"]:
        vrk.append([maaned_navn(m).split()[0].capitalize(), "–" if kwh is None else dk(kwh, 0),
                    "–" if kr is None else f"{dk(kr, 0)} kr"])
    gjort = [v for v in d["vinter"] if v[1] is not None]
    vrk.append(["Sæsonen i alt", dk(sum(v[1] for v in gjort), 0), f"{dk(sum(v[2] for v in gjort), 0)} kr"])
    halv = (BREDDE - 8 * mm) / 2
    vt = tabel(vrk, [halv * 0.42, halv * 0.25, halv * 0.33], hoejre=(1, 2), sum_raekke=True)
    if len(gjort) > 1:
        hoejre_side = soejler([v[1] for v in gjort], [MND_KORT[int(v[0][5:]) - 1] for v in gjort], halv, 112, 1, "kWh")
    else:
        hoejre_side = Paragraph("Grafen over sæsonen vises fra anden afregningsmåned.<br/><br/>"
                                + ("Sammenligning med samme måned sidste år vises, når der er data fra sidste år."
                                   if not d["sidste_aar"] else ""), S["lille"])
    to = Table([[vt, hoejre_side]], colWidths=[halv + 8 * mm, halv])
    to.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (0, 0), 8 * mm), ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
    fl.append(KeepTogether([Paragraph(f"Vintersæsonen {d['saeson']} indtil nu", S["h2"]), to]))

    # side 2
    fl.append(PageBreak())
    fl.append(Paragraph("Største forbrug i måneden", S["h2"]))
    trk = [["#", "MÅLER", "PLACERING", "BÅD", "MEDLEM", "KWH", "GRUPPE", "PRIS"]]
    baggrunde = []
    for i, r in enumerate(d["top"], start=1):
        celle, lys = gruppe_celle(r["gruppe"], r["label"])
        baggrunde.append(("BACKGROUND", (6, i), (6, i), lys))
        trk.append([str(i), f'{r["maaler_id"]:03d}', r["placering"] or "", Paragraph(escape(r["baad"] or ""), S["celle"]),
                    Paragraph(escape(r["navn"] or ""), S["celle"]), dk(r["kwh"], 1), celle, f'{dk(r["pris"] or 0, 0)} kr'])
    tt = tabel(trk, [8 * mm, 14 * mm, 20 * mm, 32 * mm, 42 * mm, 18 * mm, 28 * mm, BREDDE - 162 * mm],
               hoejre=(5, 7))
    tt.setStyle(TableStyle(baggrunde))
    fl.append(tt)
    fl.append(Spacer(1, 3))
    fl.append(Paragraph(f'De {len(d["top"])} største står for {dk(d["top_andel"], 0)} % af månedens samlede forbrug.',
                        S["lille"]))

    dr = d["drift"]
    fl.append(Paragraph("Drift og datakvalitet", S["h2"]))
    venstre = [["Målere online nu", f'{dr["online"]} af {dr["alle"]}'],
               ["Målinger modtaget (dataopsamling)", "–" if dr["data"] is None else f'{dk(dr["data"], 1)} %'],
               ["Målere offline mere end 1 time", f'{dr["offline"]} {"gang" if dr["offline"] == 1 else "gange"}'],
               ["Udfald i forbindelsen til Home Assistant", str(dr["ha"])],
               ["Natlig backup", f'{dr["backup"]} af {dr["dage_i_maaned"]} nætter']]
    hoejre = [["Nye tildelinger i måneden", str(dr["nye"])],
              ["Afsluttede tildelinger", str(dr["afsluttet"])],
              ["Tildelte målere uden forbrug i 30 dage", str(dr["ubrugte"])],
              ["Ledige målere", str(dr["ledige"])]]
    to = Table([[tabel(venstre, [halv * 0.7, halv * 0.3], hoejre=(1,), hoved=False),
                 tabel(hoejre, [halv * 0.8, halv * 0.2], hoejre=(1,), hoved=False)]],
               colWidths=[halv + 8 * mm, halv])
    to.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (0, 0), 8 * mm), ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
    fl.append(to)
    fl.append(Spacer(1, 8))

    n, kwh = d["uden_bruger"]
    if n:
        fl += boks(f"<b>OBS:</b> {n} målere har haft forbrug uden en tildelt bruger ({dk(kwh, 1)} kWh i alt). "
                   "Forbruget kan ikke opkræves, før havnekontoret har tildelt målerne.", "#c77d00", "#fff8ea")
    else:
        fl += boks("Alt forbrug i måneden er knyttet til en båd.", "#1f8a4c", "#effaf3")
    if dr["ubrugte"]:
        fl += boks(f"<b>Til havnekontoret:</b> {dr['ubrugte']} tildelte målere har ikke haft forbrug i 30 dage. "
                   "Se listen under Brugere › \"Målere der ikke er i brug\" og overvej at frigive dem.",
                   "#c77d00", "#fff8ea")
    if dr["backup"] < dr["dage_i_maaned"]:
        fl += boks(f"<b>Backup:</b> der er kun taget backup {dr['backup']} af {dr['dage_i_maaned']} nætter. "
                   "Systemadministratoren bør se på System › Log.", "#c77d00", "#fff8ea")
    if d["sidste_i_saeson"]:
        fl += boks(f"<b>Sæsonafslutning:</b> Vinteren {d['saeson']} er afsluttet. Opgørelse pr. båd for hele sæsonen "
                   f"({dk(sum(v[2] for v in gjort), 0)} kr i alt) kan hentes som CSV under Opgørelse › "
                   "\"Hele vinterperioden pr. bruger\", til opkrævning ved standerhejsning.", "#1f8a4c", "#effaf3")
    if not d["laast"]:
        fl += boks("<b>Bemærk:</b> måneden er ikke låst endnu, så tallene er foreløbige.", "#c77d00", "#fff8ea")

    fl.append(Paragraph("Vedhæftet", S["h2"]))
    fl.append(Paragraph(f'elforbrug-{d["maaned"]}.csv – fuld opgørelse pr. måler og båd (kan åbnes i Excel).',
                        S["lille"]))

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=V, rightMargin=V, topMargin=32 * mm, bottomMargin=16 * mm,
                          title=f"Månedsrapport {d['titel']} – elmålere", author=config.KLUB_NAVN)
    ramme = Frame(V, 16 * mm, BREDDE, H - 32 * mm - 16 * mm, id="indhold", leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="side", frames=[ramme], onPage=side)])
    doc.build(fl)
    return buf.getvalue()


def mailtekst(d):
    linjer = [f"Hej bestyrelse,", "",
              f"Hermed månedsrapporten for elmålerne – {maaned_navn(d['maaned'])}.", "",
              f"Samlet forbrug: {dk(d['total'], 0)} kWh",
              f"Til opkrævning: {dk(d['kr'], 0)} kr fordelt på {d['baade']} både", ""]
    if d["uden_bruger"][0]:
        linjer += [f"OBS: {d['uden_bruger'][0]} målere har forbrug uden tildelt bruger.", ""]
    linjer += ["Rapporten er vedhæftet som PDF, og den fulde opgørelse pr. båd som CSV (Excel).", "",
               "Venlig hilsen", f"{config.KLUB_NAVN} – elmålerne (automatisk mail)"]
    return "\n".join(linjer)
