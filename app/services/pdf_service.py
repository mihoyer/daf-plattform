"""PDF-Service: Erstellt vollständige Einstufungsberichte mit ReportLab."""
import io
import math
import tempfile
import os
from datetime import datetime
from typing import Optional

import qrcode
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image as PILImage

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle, KeepTogether,
)

from app.models.database import TestSession, CEFRNiveau, ModulStatus, ModulTyp

# ── Farben ─────────────────────────────────────────────────────────────────
DUNKELBLAU  = colors.HexColor("#1a2744")
MITTELBLAU  = colors.HexColor("#2563eb")
HELLBLAU    = colors.HexColor("#eff6ff")
GRUEN       = colors.HexColor("#16a34a")
ORANGE      = colors.HexColor("#d97706")
ROT         = colors.HexColor("#dc2626")
GRAU        = colors.HexColor("#6b7280")
HELLGRAU    = colors.HexColor("#f3f4f6")

NIVEAU_FARBEN = {
    "A1": colors.HexColor("#ef4444"), "A2": colors.HexColor("#f97316"),
    "B1": colors.HexColor("#eab308"), "B2": colors.HexColor("#22c55e"),
    "C1": colors.HexColor("#3b82f6"), "C2": colors.HexColor("#8b5cf6"),
}
NIVEAU_FARBEN_HEX = {
    "A1": "#ef4444", "A2": "#f97316", "B1": "#eab308",
    "B2": "#22c55e", "C1": "#3b82f6", "C2": "#8b5cf6",
}
CEFR_WERT = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}

MODUL_NAMEN = {
    "m1_grammatik":     "M1 – Grammatik & Wortschatz",
    "m2_lesen":         "M2 – Lesen & Leseverstehen",
    "m3_hoerverstehen": "M3 – Hörverstehen",
    "m4_vorlesen":      "M4 – Vorlesen",
    "m5_sprechen":      "M5 – Sprechen",
    "m6_schreiben":     "M6 – Schreiben",
}
MODUL_KURZ = {
    "m1_grammatik": "Grammatik", "m2_lesen": "Lesen",
    "m3_hoerverstehen": "Hören", "m4_vorlesen": "Vorlesen",
    "m5_sprechen": "Sprechen", "m6_schreiben": "Schreiben",
}

PAGE_W = A4[0]
PAGE_H = A4[1]
MARGIN = 2 * cm
CONTENT_W = PAGE_W - 2 * MARGIN   # ~17 cm


# ── Hilfsfunktionen ────────────────────────────────────────────────────────

def _qr_image(url: str, size_cm: float = 2.8) -> Image:
    """Erstellt einen QR-Code als ReportLab-Image."""
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    size = size_cm * cm
    return Image(buf, width=size, height=size)


def _radar_image(labels: list, values: list, farben: list, width_cm=13.0, height_cm=13.0) -> Image:
    """Erstellt ein grosses, professionelles Radar-Diagramm mit 0–100-Skala."""
    N = len(labels)
    angles = [n / float(N) * 2 * math.pi for n in range(N)]
    angles += angles[:1]
    vals = list(values) + [values[0]]

    fig, ax = plt.subplots(
        figsize=(width_cm / 2.54, height_cm / 2.54),
        subplot_kw=dict(polar=True)
    )
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#f8fafc")

    # Hintergrund-Ringe (Zonen) für visuelle Orientierung
    zone_farben = ["#fee2e2", "#fef3c7", "#d1fae5", "#dbeafe", "#ede9fe"]
    for i, rf in enumerate(zone_farben, 1):
        rv = i * 20
        ax.fill(angles, [rv] * (N + 1), alpha=0.10, color=rf, zorder=1)

    # Gitternetzlinien (Ringe)
    ax.set_ylim(0, 100)
    ax.set_yticks([20, 40, 60, 80, 100])
    ax.set_yticklabels(["20", "40", "60", "80", "100"],
                       fontsize=8, color="#9ca3af", fontweight="normal")
    ax.set_rlabel_position(15)  # Zahlen leicht versetzt von der ersten Speiche
    ax.grid(color="#d1d5db", linewidth=0.7, linestyle="--", zorder=1)

    # Datenfläche
    ax.plot(angles, vals, color="#2563eb", linewidth=2.5, linestyle="solid", zorder=3)
    ax.fill(angles, vals, alpha=0.20, color="#2563eb", zorder=2)

    # Datenpunkte farbig nach Niveau
    for angle, val, farbe in zip(angles[:-1], values, farben):
        ax.plot(angle, val, "o", color=farbe, markersize=11, zorder=5,
                markeredgecolor="white", markeredgewidth=2)

    # Modul-Namen aussen
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels, fontsize=11, fontweight="bold", color="#1a2744")
    ax.tick_params(pad=20)

    ax.spines["polar"].set_color("#d1d5db")
    ax.spines["polar"].set_linewidth(1)

    plt.tight_layout(pad=1.5)
    buf = io.BytesIO()
    plt.savefig(buf, format="PNG", dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return Image(buf, width=width_cm * cm, height=height_cm * cm)


def _tabelle(story, rows, col_widths=None):
    """Tabelle mit automatischer Spaltenbreite und Zeilenumbruch."""
    col_count = len(rows[0])
    if col_widths is None:
        if col_count == 3:
            # Kategorie | Score | Begründung
            col_widths = [3.8 * cm, 1.8 * cm, CONTENT_W - 5.6 * cm]
        elif col_count == 4:
            # Nr | Frage | Antwort | Korrekt
            col_widths = [1.0 * cm, 5.5 * cm, 8.5 * cm, 2.0 * cm]
        else:
            equal = CONTENT_W / col_count
            col_widths = [equal] * col_count

    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors as rl_colors

    # Zellen als Paragraph für Zeilenumbruch
    wrapped_rows = []
    body_s = ParagraphStyle("tbl_body", fontSize=8.5, leading=12, fontName="Helvetica",
                             textColor=rl_colors.black, wordWrap="CJK")
    head_s = ParagraphStyle("tbl_head", fontSize=8.5, leading=12, fontName="Helvetica-Bold",
                             textColor=rl_colors.white, wordWrap="CJK")
    for r_idx, row in enumerate(rows):
        s = head_s if r_idx == 0 else body_s
        wrapped_rows.append([Paragraph(str(cell), s) for cell in row])

    t = Table(wrapped_rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  rl_colors.HexColor("#1a2744")),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 8.5),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [rl_colors.white, rl_colors.HexColor("#f9fafb")]),
        ("GRID",          (0, 0), (-1, -1), 0.3, rl_colors.HexColor("#e5e7eb")),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(t)
    story.append(Spacer(1, 6))


# ── Haupt-PDF-Funktion ─────────────────────────────────────────────────────

async def erstelle_pdf(sess: TestSession) -> bytes:
    buffer = io.BytesIO()

    COPYRIGHT = "Konzept, Didaktik & Entwicklung © Iryna Hoyer  |  DaF/DaZ Sprachdiagnostik"
    EINSTIEG_URL = "https://daf-plattform.mikehoyer.de"

    # QR-Code einmalig erzeugen
    qr_img = _qr_image(EINSTIEG_URL, size_cm=2.5)

    def _erste_seite(canvas, doc):
        """Kopf- und Fusszeile auf der ersten Seite."""
        canvas.saveState()
        # QR-Code oben rechts
        qr_x = PAGE_W - MARGIN - 2.5 * cm
        qr_y = PAGE_H - MARGIN - 2.5 * cm
        qr_img.drawOn(canvas, qr_x, qr_y)
        canvas.setFont("Helvetica", 6)
        canvas.setFillColor(GRAU)
        canvas.drawRightString(PAGE_W - MARGIN, qr_y - 8, "Zum Test scannen")
        # Fusszeile
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GRAU)
        canvas.drawString(MARGIN, 1.2 * cm, COPYRIGHT)
        canvas.drawRightString(PAGE_W - MARGIN, 1.2 * cm,
                               f"Seite 1  |  {datetime.now().strftime('%d.%m.%Y')}")
        canvas.restoreState()

    def _folgeseiten(canvas, doc):
        """Kopf- und Fusszeile auf Folgeseiten."""
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(DUNKELBLAU)
        canvas.drawString(MARGIN, PAGE_H - 1.4 * cm, "DaF/DaZ Sprachdiagnostik – Einstufungsbericht")
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GRAU)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 1.4 * cm,
                               f"Session: {sess.token[:8]}…")
        canvas.line(MARGIN, PAGE_H - 1.6 * cm, PAGE_W - MARGIN, PAGE_H - 1.6 * cm)
        # Fusszeile
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GRAU)
        canvas.drawString(MARGIN, 1.2 * cm, COPYRIGHT)
        canvas.drawRightString(PAGE_W - MARGIN, 1.2 * cm,
                               f"Seite {doc.page}  |  {datetime.now().strftime('%d.%m.%Y')}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=2.5 * cm, bottomMargin=2.0 * cm,
    )

    styles = getSampleStyleSheet()
    titel_style    = ParagraphStyle("Titel",    fontSize=20, textColor=DUNKELBLAU,
                                    spaceAfter=4, fontName="Helvetica-Bold")
    untertitel_style = ParagraphStyle("Untertitel", fontSize=10, textColor=GRAU,
                                      spaceAfter=2, fontName="Helvetica")
    h2_style       = ParagraphStyle("H2",       fontSize=13, textColor=DUNKELBLAU,
                                    spaceBefore=14, spaceAfter=5, fontName="Helvetica-Bold")
    h3_style       = ParagraphStyle("H3",       fontSize=10.5, textColor=MITTELBLAU,
                                    spaceBefore=8, spaceAfter=3, fontName="Helvetica-Bold")
    body_style     = ParagraphStyle("Body",     fontSize=9.5, textColor=colors.black,
                                    spaceAfter=4, fontName="Helvetica", leading=14)
    klein_style    = ParagraphStyle("Klein",    fontSize=7.5, textColor=GRAU,
                                    spaceAfter=2, fontName="Helvetica")
    hinweis_style  = ParagraphStyle("Hinweis",  fontSize=8, textColor=colors.HexColor("#1e40af"),
                                    spaceAfter=4, fontName="Helvetica",
                                    backColor=colors.HexColor("#eff6ff"), leading=12,
                                    leftIndent=6, rightIndent=6)

    story = []

    # ── Kopfzeile (Platz für QR-Code rechts lassen) ────────────────────────
    # Titel-Tabelle: Text links, Platz rechts für QR (der wird via canvas gezeichnet)
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph("DaF/DaZ Sprachdiagnostik", titel_style))
    story.append(Paragraph("Einstufungsbericht – Vertraulich", untertitel_style))
    story.append(Paragraph(
        f"Erstellt am: {datetime.now().strftime('%d.%m.%Y %H:%M')} &nbsp;|&nbsp; Session: {sess.token[:8]}…",
        klein_style
    ))
    story.append(Paragraph(
        "KI-generierter Bericht – gelegentliche inhaltliche Ungenauigkeiten sind möglich.",
        hinweis_style
    ))
    story.append(HRFlowable(width="100%", thickness=2, color=MITTELBLAU, spaceAfter=10))

    # ── Gesamtergebnis ─────────────────────────────────────────────────────
    story.append(Paragraph("Gesamtergebnis", h2_style))

    niveau = (sess.gesamt_niveau.value if sess.gesamt_niveau
              else (sess.grob_niveau.value if sess.grob_niveau else "–"))
    niveau_farbe = NIVEAU_FARBEN.get(niveau, GRAU)
    score_val = f"{sess.gesamt_score:.0f}" if sess.gesamt_score else "–"
    paket_val = sess.paket.value.capitalize() if sess.paket else "–"
    hilfs_val = sess.hilfssprache.value.upper() if sess.hilfssprache else "–"

    # Niveau-Badge gross, Score gross – als Paragraph in Tabellenzelle
    niv_style = ParagraphStyle("Niv", fontSize=26, fontName="Helvetica-Bold",
                                textColor=colors.white, alignment=TA_CENTER, leading=30)
    score_big_style = ParagraphStyle("ScoreBig", fontSize=22, fontName="Helvetica-Bold",
                                      textColor=niveau_farbe, alignment=TA_CENTER, leading=26)
    label_style = ParagraphStyle("Label", fontSize=8.5, fontName="Helvetica-Bold",
                                  textColor=colors.white, alignment=TA_CENTER)
    label_dark = ParagraphStyle("LabelDark", fontSize=8.5, fontName="Helvetica-Bold",
                                 textColor=DUNKELBLAU, alignment=TA_CENTER)

    gesamt_data = [
        [Paragraph("CEFR-Niveau", label_style),
         Paragraph("Gesamtscore", label_style),
         Paragraph("Paket", label_dark),
         Paragraph("Hilfssprache", label_dark)],
        [Paragraph(niveau, niv_style),
         Paragraph(f"{score_val}/100", score_big_style),
         Paragraph(paket_val, body_style),
         Paragraph(hilfs_val, body_style)],
    ]
    col_w = CONTENT_W / 4
    gesamt_table = Table(gesamt_data, colWidths=[col_w] * 4)
    gesamt_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (1, 0),  DUNKELBLAU),
        ("BACKGROUND",    (2, 0), (-1, 0), HELLBLAU),
        ("BACKGROUND",    (0, 1), (0, 1),  niveau_farbe),
        ("BACKGROUND",    (1, 1), (1, 1),  HELLBLAU),
        ("BACKGROUND",    (2, 1), (-1, 1), colors.white),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(gesamt_table)
    story.append(Spacer(1, 10))

    # ── Gesamteinschätzung ─────────────────────────────────────────────────
    if getattr(sess, 'gesamt_einschaetzung', None):
        story.append(Paragraph("Gesamteinschätzung", h2_style))
        story.append(Paragraph(sess.gesamt_einschaetzung, body_style))
        story.append(Spacer(1, 6))

    # ── Radar-Diagramm + Modul-Übersicht nebeneinander ─────────────────────
    abgeschlossene = [m for m in sorted(sess.module, key=lambda x: x.reihenfolge)
                      if m.status == ModulStatus.abgeschlossen]

    if len(abgeschlossene) >= 2:
        story.append(Paragraph("Kompetenzprofil", h2_style))

        radar_labels, radar_vals, radar_farben = [], [], []
        for m in abgeschlossene:
            cefr_str = m.cefr_niveau.value if m.cefr_niveau else ""
            raw = round(m.gesamt_score or 0)
            if raw <= 15:
                raw *= 10
            raw = min(100, raw)
            radar_labels.append(MODUL_KURZ.get(m.modul.value, m.modul.value))
            radar_vals.append(raw)  # 0–100
            radar_farben.append(NIVEAU_FARBEN_HEX.get(cefr_str, "#6b7280"))

        # Radar zentriert, gross, allein in einer Zeile
        radar_img = _radar_image(radar_labels, radar_vals, radar_farben,
                                  width_cm=13.0, height_cm=13.0)
        radar_tbl = Table([[radar_img]], colWidths=[CONTENT_W])
        radar_tbl.setStyle(TableStyle([
            ("ALIGN",  (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",  (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING",   (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
        ]))
        story.append(radar_tbl)
        story.append(Spacer(1, 10))

        # Modul-Übersicht als kompakte Tabelle darunter
        mh_s = ParagraphStyle("mh", fontSize=8.5, fontName="Helvetica-Bold", textColor=colors.white, alignment=TA_CENTER)
        mb_s = ParagraphStyle("mb", fontSize=8.5, fontName="Helvetica")
        modul_rows = [[
            Paragraph("Modul", mh_s),
            Paragraph("CEFR-Niveau", mh_s),
            Paragraph("Score", mh_s),
        ]]
        for m in abgeschlossene:
            cefr_str = m.cefr_niveau.value if m.cefr_niveau else "–"
            nf = NIVEAU_FARBEN.get(cefr_str, GRAU)
            sc = f"{m.gesamt_score:.0f}" if m.gesamt_score else "–"
            modul_rows.append([
                Paragraph(MODUL_KURZ.get(m.modul.value, m.modul.value), mb_s),
                Paragraph(cefr_str, ParagraphStyle("mc", fontSize=9.5, fontName="Helvetica-Bold",
                                                    textColor=nf, alignment=TA_CENTER)),
                Paragraph(f"{sc}/100", ParagraphStyle("ms", fontSize=8.5, fontName="Helvetica",
                                                       alignment=TA_CENTER)),
            ])

        col_w3 = CONTENT_W / 3
        modul_tbl = Table(modul_rows, colWidths=[col_w3, col_w3, col_w3])
        modul_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), DUNKELBLAU),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, HELLGRAU]),
            ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(modul_tbl)
        story.append(Spacer(1, 8))

    # ── Detailberichte pro Modul ───────────────────────────────────────────
    for m in sorted(sess.module, key=lambda x: x.reihenfolge):
        if m.status != ModulStatus.abgeschlossen:
            continue
        analyse = m.get_ki_analyse()
        if not analyse:
            continue

        story.append(PageBreak())

        cefr = m.cefr_niveau.value if m.cefr_niveau else "–"
        score = f"{m.gesamt_score:.0f}" if m.gesamt_score else "–"
        nf = NIVEAU_FARBEN.get(cefr, GRAU)

        # Modul-Header mit farbigem CEFR-Badge
        modul_name = MODUL_NAMEN.get(m.modul.value, m.modul.value)
        badge_style = ParagraphStyle("Badge", fontSize=14, fontName="Helvetica-Bold",
                                      textColor=colors.white, alignment=TA_CENTER)
        score_style = ParagraphStyle("ScoreH", fontSize=14, fontName="Helvetica-Bold",
                                      textColor=nf, alignment=TA_RIGHT)
        header_tbl = Table(
            [[Paragraph(modul_name, h2_style),
              Paragraph(cefr, badge_style),
              Paragraph(f"{score}/100", score_style)]],
            colWidths=[CONTENT_W - 4*cm, 1.8*cm, 2.2*cm],
        )
        header_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (1, 0), (1, 0), nf),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (0, 0), 0),
            ("RIGHTPADDING",  (-1, 0), (-1, 0), 0),
        ]))
        story.append(header_tbl)
        story.append(HRFlowable(width="100%", thickness=1, color=HELLBLAU, spaceAfter=8))

        if m.modul.value == "m1_grammatik":
            _pdf_m1(story, analyse, h3_style, body_style, klein_style)
        elif m.modul.value in ("m2_lesen", "m3_hoerverstehen"):
            _pdf_mc(story, analyse, h3_style, body_style)
        elif m.modul.value == "m4_vorlesen":
            _pdf_vorlesen(story, analyse, h3_style, body_style)
        elif m.modul.value == "m5_sprechen":
            _pdf_sprechen(story, analyse, h3_style, body_style, klein_style)
        elif m.modul.value == "m6_schreiben":
            _pdf_schreiben(story, analyse, h3_style, body_style, klein_style)

    # ── Abschluss-Fusszeile ────────────────────────────────────────────────
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GRAU, spaceAfter=6))
    story.append(Paragraph(
        "Datenschutzhinweis: Dieser Bericht enthält keine personenbezogenen Daten. "
        "Audio- und Bilddateien wurden nach der Analyse automatisch gelöscht. "
        "Die Analyse basiert ausschließlich auf anonymisierten Sprachproben.",
        klein_style
    ))
    story.append(Spacer(1, 4))
    story.append(Paragraph(COPYRIGHT, klein_style))

    doc.build(story, onFirstPage=_erste_seite, onLaterPages=_folgeseiten)
    return buffer.getvalue()


# ── Modul-Detail-Funktionen ────────────────────────────────────────────────

def _pdf_m1(story, analyse, h3_style, body_style, klein_style):
    # M1 speichert Detailergebnisse in 'details' (MC-Format)
    if "details" in analyse:
        rows = [["Frage", "Gewählte Antwort", "✓?"]]
        for d in analyse["details"]:
            antwort = d.get("gewaehlt_text") or d.get("korrekt_text") or ""
            korrekt_icon = "✓" if d.get("ist_korrekt") else "✗"
            rows.append([
                d.get("frage", "")[:80],
                antwort,
                korrekt_icon,
            ])
        _tabelle(story, rows, col_widths=[7.0*cm, 7.5*cm, 2.5*cm])
    # Detailkategorien wenn vorhanden
    for section, title in [("grammatik", "Grammatik"), ("wortschatz", "Wortschatz")]:
        if section in analyse and isinstance(analyse[section], dict):
            story.append(Paragraph(title, h3_style))
            rows = [["Kategorie", "Score", "Begründung"]]
            for key, val in analyse[section].items():
                if isinstance(val, dict):
                    rows.append([
                        key.replace("_", " ").capitalize(),
                        f"{val.get('score', 0)}/10",
                        val.get("begruendung", ""),
                    ])
            if len(rows) > 1:
                _tabelle(story, rows)
    _pdf_staerken_schwaechen(story, analyse, body_style)


def _pdf_mc(story, analyse, h3_style, body_style):
    story.append(Paragraph(
        f"Ergebnis: <b>{analyse.get('korrekt', 0)}/{analyse.get('total', 0)}</b> korrekt",
        body_style
    ))
    if "details" in analyse:
        rows = [["Frage", "Gewählte Antwort", "✓?"]]
        for d in analyse["details"]:
            # Feldnamen: gewaehlt_text oder antwort
            antwort = d.get("gewaehlt_text") or d.get("antwort") or ""
            korrekt_icon = "✓" if d.get("ist_korrekt") else "✗"
            rows.append([
                d.get("frage", ""),
                antwort,
                korrekt_icon,
            ])
        _tabelle(story, rows, col_widths=[7.0*cm, 7.5*cm, 2.5*cm])


def _pdf_vorlesen(story, analyse, h3_style, body_style):
    story.append(Paragraph(
        f"Lesegenauigkeit: <b>{analyse.get('lesegenauigkeit', 0)}%</b>", body_style
    ))
    if analyse.get("audio_analyse"):
        aa = analyse["audio_analyse"]
        rows = [["Kategorie", "Score", "Begründung"]]
        for key in ("aussprache", "prosodie_betonung", "rhythmus", "fluessigkeit"):
            if key in aa:
                rows.append([
                    key.replace("_", " ").capitalize(),
                    f"{aa[key].get('score', 0)}/10",
                    aa[key].get("begruendung", ""),
                ])
        _tabelle(story, rows)
        if aa.get("zusammenfassung"):
            story.append(Paragraph(f"<b>Zusammenfassung:</b> {aa['zusammenfassung']}", body_style))


def _pdf_sprechen(story, analyse, h3_style, body_style, klein_style):
    text_analyse = analyse.get("text_analyse", {})
    if text_analyse:
        for section, title in [("grammatik", "Grammatik"), ("wortschatz", "Wortschatz")]:
            if section in text_analyse:
                story.append(Paragraph(title, h3_style))
                rows = [["Kategorie", "Score", "Begründung"]]
                for key, val in text_analyse[section].items():
                    rows.append([
                        key.replace("_", " ").capitalize(),
                        f"{val.get('score', 0)}/10",
                        val.get("begruendung", ""),
                    ])
                _tabelle(story, rows)
        if text_analyse.get("gesamteinschaetzung"):
            story.append(Paragraph(
                f"<b>Gesamteinschätzung:</b> {text_analyse['gesamteinschaetzung']}", body_style
            ))
        _pdf_staerken_schwaechen(story, text_analyse, body_style)

    audio_analyse = analyse.get("audio_analyse")
    if audio_analyse:
        story.append(Paragraph("Aussprache-Analyse", h3_style))
        rows = [["Kategorie", "Score", "Begründung"]]
        for key in ("verstaendlichkeit", "fluss_fluency", "akzent", "intonation"):
            if key in audio_analyse:
                rows.append([
                    key.replace("_", " ").capitalize(),
                    f"{audio_analyse[key].get('score', 0)}/10",
                    audio_analyse[key].get("begruendung", ""),
                ])
        _tabelle(story, rows)
        if audio_analyse.get("zusammenfassung"):
            story.append(Paragraph(
                f"<b>Zusammenfassung:</b> {audio_analyse['zusammenfassung']}", body_style
            ))

    if analyse.get("transkript"):
        story.append(Paragraph("Transkript", h3_style))
        story.append(Paragraph(analyse["transkript"], body_style))


def _pdf_schreiben(story, analyse, h3_style, body_style, klein_style):
    if analyse.get("transkript"):
        story.append(Paragraph("Erkannter Text (Handschrift)", h3_style))
        story.append(Paragraph(analyse["transkript"], body_style))

    rows = [["Kategorie", "Score", "Begründung"]]
    for key in ("grammatik", "wortschatz", "satzbau", "kohaerenz",
                "aufgabenerfullung", "rechtschreibung", "lesbarkeit"):
        if key in analyse:
            val = analyse[key]
            if isinstance(val, dict):
                rows.append([
                    key.replace("_", " ").capitalize(),
                    f"{val.get('score', 0)}/10",
                    val.get("begruendung", ""),
                ])
    if len(rows) > 1:
        _tabelle(story, rows)

    if analyse.get("gesamteinschaetzung"):
        story.append(Paragraph(
            f"<b>Gesamteinschätzung:</b> {analyse['gesamteinschaetzung']}", body_style
        ))
    _pdf_staerken_schwaechen(story, analyse, body_style)


def _pdf_staerken_schwaechen(story, analyse, body_style):
    gruen_s = ParagraphStyle("Gruen", parent=body_style, textColor=colors.HexColor("#16a34a"))
    orange_s = ParagraphStyle("Orange", parent=body_style, textColor=colors.HexColor("#d97706"))
    blau_s = ParagraphStyle("Blau", parent=body_style, textColor=colors.HexColor("#2563eb"))

    if analyse.get("staerken"):
        story.append(Paragraph("<b>Stärken:</b>", body_style))
        for s in analyse["staerken"]:
            story.append(Paragraph(f"✓ {s}", gruen_s))
    if analyse.get("schwaechen"):
        story.append(Paragraph("<b>Verbesserungspotenzial:</b>", body_style))
        for s in analyse["schwaechen"]:
            story.append(Paragraph(f"→ {s}", orange_s))
    if analyse.get("empfehlungen"):
        story.append(Paragraph("<b>Empfehlungen:</b>", body_style))
        for e in analyse["empfehlungen"]:
            story.append(Paragraph(f"📌 {e}", blau_s))
