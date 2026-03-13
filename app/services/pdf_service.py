"""PDF-Service: Erstellt vollständige Einstufungsberichte mit ReportLab."""
import io
from datetime import datetime
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from app.models.database import TestSession, CEFRNiveau, ModulStatus, ModulTyp

# Farben
DUNKELBLAU = colors.HexColor("#1a2744")
MITTELBLAU = colors.HexColor("#2563eb")
HELLBLAU = colors.HexColor("#eff6ff")
GRUEN = colors.HexColor("#16a34a")
ORANGE = colors.HexColor("#d97706")
ROT = colors.HexColor("#dc2626")
GRAU = colors.HexColor("#6b7280")
HELLGRAU = colors.HexColor("#f3f4f6")

NIVEAU_FARBEN = {
    "A1": colors.HexColor("#ef4444"), "A2": colors.HexColor("#f97316"),
    "B1": colors.HexColor("#eab308"), "B2": colors.HexColor("#22c55e"),
    "C1": colors.HexColor("#3b82f6"), "C2": colors.HexColor("#8b5cf6"),
}

MODUL_NAMEN = {
    "m1_grammatik": "M1 – Grammatik & Wortschatz",
    "m2_lesen": "M2 – Lesen & Leseverstehen",
    "m3_hoerverstehen": "M3 – Hörverstehen",
    "m4_vorlesen": "M4 – Vorlesen",
    "m5_sprechen": "M5 – Sprechen",
    "m6_schreiben": "M6 – Schreiben",
}


async def erstelle_pdf(sess: TestSession) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm,
    )

    styles = getSampleStyleSheet()
    story = []

    # Styles definieren
    titel_style = ParagraphStyle("Titel", parent=styles["Normal"], fontSize=22, textColor=DUNKELBLAU,
                                  spaceAfter=4, fontName="Helvetica-Bold", alignment=TA_LEFT)
    untertitel_style = ParagraphStyle("Untertitel", parent=styles["Normal"], fontSize=11, textColor=GRAU,
                                       spaceAfter=2, fontName="Helvetica")
    h2_style = ParagraphStyle("H2", parent=styles["Normal"], fontSize=14, textColor=DUNKELBLAU,
                               spaceBefore=16, spaceAfter=6, fontName="Helvetica-Bold")
    h3_style = ParagraphStyle("H3", parent=styles["Normal"], fontSize=11, textColor=MITTELBLAU,
                               spaceBefore=10, spaceAfter=4, fontName="Helvetica-Bold")
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=9.5, textColor=colors.black,
                                 spaceAfter=4, fontName="Helvetica", leading=14)
    klein_style = ParagraphStyle("Klein", parent=styles["Normal"], fontSize=8, textColor=GRAU,
                                  spaceAfter=2, fontName="Helvetica")

    # ── Kopfzeile ──────────────────────────────────────────────────────────
    story.append(Paragraph("DaF Sprachdiagnostik", titel_style))
    story.append(Paragraph("Einstufungsbericht – Vertraulich", untertitel_style))
    story.append(Paragraph(f"Erstellt am: {datetime.now().strftime('%d.%m.%Y %H:%M')} | Session: {sess.token[:8]}...", klein_style))
    story.append(HRFlowable(width="100%", thickness=2, color=MITTELBLAU, spaceAfter=12))

    # ── Gesamtergebnis ─────────────────────────────────────────────────────
    story.append(Paragraph("Gesamtergebnis", h2_style))

    niveau = sess.gesamt_niveau.value if sess.gesamt_niveau else (sess.grob_niveau.value if sess.grob_niveau else "–")
    niveau_farbe = NIVEAU_FARBEN.get(niveau, GRAU)
    score = f"{sess.gesamt_score:.1f}" if sess.gesamt_score else "–"

    gesamt_data = [
        ["CEFR-Niveau", "Gesamtscore", "Paket", "Hilfssprache"],
        [niveau, f"{score} / 100", sess.paket.value.capitalize(), sess.hilfssprache.value.upper()],
    ]
    gesamt_table = Table(gesamt_data, colWidths=[4*cm, 4*cm, 4*cm, 4*cm])
    gesamt_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DUNKELBLAU),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("BACKGROUND", (0, 1), (0, 1), niveau_farbe),
        ("TEXTCOLOR", (0, 1), (0, 1), colors.white),
        ("FONTNAME", (0, 1), (0, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (0, 1), 18),
        ("FONTSIZE", (1, 1), (1, 1), 14),
        ("FONTNAME", (1, 1), (1, 1), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (1, 1), (-1, -1), [HELLBLAU, colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
    ]))
    story.append(gesamt_table)
    story.append(Spacer(1, 12))

    # ── Modul-Übersicht ────────────────────────────────────────────────────
    story.append(Paragraph("Modul-Übersicht", h2_style))

    modul_header = ["Modul", "Status", "CEFR", "Score"]
    modul_rows = [modul_header]
    for m in sorted(sess.module, key=lambda x: x.reihenfolge):
        status_text = "✓ Abgeschlossen" if m.status == ModulStatus.abgeschlossen else "○ Ausstehend"
        cefr_text = m.cefr_niveau.value if m.cefr_niveau else "–"
        score_text = f"{m.gesamt_score:.1f}" if m.gesamt_score else "–"
        modul_rows.append([MODUL_NAMEN.get(m.modul.value, m.modul.value), status_text, cefr_text, score_text])

    modul_table = Table(modul_rows, colWidths=[8*cm, 4*cm, 2.5*cm, 2.5*cm])
    modul_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DUNKELBLAU),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, HELLGRAU]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(modul_table)
    story.append(Spacer(1, 8))

    # ── Detailberichte pro Modul ───────────────────────────────────────────
    for m in sorted(sess.module, key=lambda x: x.reihenfolge):
        if m.status != ModulStatus.abgeschlossen:
            continue

        analyse = m.get_ki_analyse()
        if not analyse:
            continue

        story.append(PageBreak())
        story.append(Paragraph(MODUL_NAMEN.get(m.modul.value, m.modul.value), h2_style))
        story.append(HRFlowable(width="100%", thickness=1, color=HELLBLAU, spaceAfter=8))

        cefr = m.cefr_niveau.value if m.cefr_niveau else "–"
        score = f"{m.gesamt_score:.1f}" if m.gesamt_score else "–"
        story.append(Paragraph(f"<b>CEFR:</b> {cefr} &nbsp;&nbsp; <b>Score:</b> {score}/100", body_style))
        story.append(Spacer(1, 6))

        # Modul-spezifische Detaildarstellung
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

    # ── DSGVO-Hinweis ──────────────────────────────────────────────────────
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GRAU))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Datenschutzhinweis: Dieser Bericht enthält keine personenbezogenen Daten. "
        "Audio- und Bilddateien wurden nach der Analyse automatisch gelöscht. "
        "Die Analyse basiert ausschließlich auf anonymisierten Sprachproben.",
        klein_style
    ))

    doc.build(story)
    return buffer.getvalue()


def _pdf_m1(story, analyse, h3_style, body_style, klein_style):
    """M1 Grammatik/Wortschatz Detail."""
    if "grammatik" in analyse:
        story.append(Paragraph("Grammatik", h3_style))
        rows = [["Kategorie", "Score", "Begründung"]]
        for key, val in analyse["grammatik"].items():
            rows.append([key.replace("_", " ").capitalize(), f"{val.get('score', 0)}/10", val.get("begruendung", "")])
        _tabelle(story, rows)

    if "wortschatz" in analyse:
        story.append(Paragraph("Wortschatz", h3_style))
        rows = [["Kategorie", "Score", "Begründung"]]
        for key, val in analyse["wortschatz"].items():
            rows.append([key.capitalize(), f"{val.get('score', 0)}/10", val.get("begruendung", "")])
        _tabelle(story, rows)

    _pdf_staerken_schwaechen(story, analyse, body_style)


def _pdf_mc(story, analyse, h3_style, body_style):
    """MC-Auswertung (Lesen/Hörverstehen)."""
    story.append(Paragraph(f"Ergebnis: {analyse.get('korrekt', 0)}/{analyse.get('total', 0)} korrekt", body_style))
    if "details" in analyse:
        rows = [["Nr.", "Frage", "Korrekt?"]]
        for d in analyse["details"]:
            rows.append([str(d.get("id", "")), d.get("frage", "")[:60], "✓" if d.get("ist_korrekt") else "✗"])
        _tabelle(story, rows)


def _pdf_vorlesen(story, analyse, h3_style, body_style):
    """Vorlesen-Detail."""
    story.append(Paragraph(f"Lesegenauigkeit: {analyse.get('lesegenauigkeit', 0)}%", body_style))
    if analyse.get("audio_analyse"):
        aa = analyse["audio_analyse"]
        rows = [["Kategorie", "Score", "Begründung"]]
        for key in ("aussprache", "prosodie_betonung", "rhythmus", "fluessigkeit"):
            if key in aa:
                rows.append([key.replace("_", " ").capitalize(), f"{aa[key].get('score', 0)}/10", aa[key].get("begruendung", "")])
        _tabelle(story, rows)
        if aa.get("zusammenfassung"):
            story.append(Paragraph(f"<b>Zusammenfassung:</b> {aa['zusammenfassung']}", body_style))


def _pdf_sprechen(story, analyse, h3_style, body_style, klein_style):
    """Sprechen-Detail."""
    text_analyse = analyse.get("text_analyse", {})
    if text_analyse:
        if "grammatik" in text_analyse:
            story.append(Paragraph("Grammatik", h3_style))
            rows = [["Kategorie", "Score", "Begründung"]]
            for key, val in text_analyse["grammatik"].items():
                rows.append([key.replace("_", " ").capitalize(), f"{val.get('score', 0)}/10", val.get("begruendung", "")])
            _tabelle(story, rows)

        if "wortschatz" in text_analyse:
            story.append(Paragraph("Wortschatz", h3_style))
            rows = [["Kategorie", "Score", "Begründung"]]
            for key, val in text_analyse["wortschatz"].items():
                rows.append([key.capitalize(), f"{val.get('score', 0)}/10", val.get("begruendung", "")])
            _tabelle(story, rows)

        if text_analyse.get("gesamteinschaetzung"):
            story.append(Paragraph(f"<b>Gesamteinschätzung:</b> {text_analyse['gesamteinschaetzung']}", body_style))

        _pdf_staerken_schwaechen(story, text_analyse, body_style)

    audio_analyse = analyse.get("audio_analyse")
    if audio_analyse:
        story.append(Paragraph("Aussprache-Analyse (Audio)", h3_style))
        rows = [["Kategorie", "Score", "Begründung"]]
        for key in ("verstaendlichkeit", "fluss_fluency", "akzent", "intonation"):
            if key in audio_analyse:
                rows.append([key.replace("_", " ").capitalize(), f"{audio_analyse[key].get('score', 0)}/10", audio_analyse[key].get("begruendung", "")])
        _tabelle(story, rows)
        if audio_analyse.get("zusammenfassung"):
            story.append(Paragraph(f"<b>Zusammenfassung:</b> {audio_analyse['zusammenfassung']}", body_style))

    if analyse.get("transkript"):
        story.append(Paragraph("Transkript", h3_style))
        story.append(Paragraph(analyse["transkript"], body_style))


def _pdf_schreiben(story, analyse, h3_style, body_style, klein_style):
    """Schreiben-Detail."""
    if analyse.get("transkript"):
        story.append(Paragraph("Erkannter Text (Handschrift)", h3_style))
        story.append(Paragraph(analyse["transkript"], body_style))

    rows = [["Kategorie", "Score", "Begründung"]]
    for key in ("grammatik", "wortschatz", "satzbau", "kohaerenz", "aufgabenerfullung", "rechtschreibung", "lesbarkeit"):
        if key in analyse:
            val = analyse[key]
            if isinstance(val, dict):
                rows.append([key.replace("_", " ").capitalize(), f"{val.get('score', 0)}/10", val.get("begruendung", "")])
    if len(rows) > 1:
        _tabelle(story, rows)

    if analyse.get("gesamteinschaetzung"):
        story.append(Paragraph(f"<b>Gesamteinschätzung:</b> {analyse['gesamteinschaetzung']}", body_style))

    _pdf_staerken_schwaechen(story, analyse, body_style)


def _pdf_staerken_schwaechen(story, analyse, body_style):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    gruen_style = ParagraphStyle("Gruen", parent=body_style, textColor=colors.HexColor("#16a34a"))
    rot_style = ParagraphStyle("Rot", parent=body_style, textColor=colors.HexColor("#dc2626"))

    if analyse.get("staerken"):
        story.append(Paragraph("<b>Stärken:</b>", body_style))
        for s in analyse["staerken"]:
            story.append(Paragraph(f"• {s}", gruen_style))

    if analyse.get("schwaechen"):
        story.append(Paragraph("<b>Schwächen:</b>", body_style))
        for s in analyse["schwaechen"]:
            story.append(Paragraph(f"• {s}", rot_style))

    if analyse.get("empfehlungen"):
        story.append(Paragraph("<b>Empfehlungen:</b>", body_style))
        for e in analyse["empfehlungen"]:
            story.append(Paragraph(f"→ {e}", body_style))


def _tabelle(story, rows):
    col_count = len(rows[0])
    if col_count == 3:
        col_widths = [4*cm, 2*cm, 11*cm]
    elif col_count == 4:
        col_widths = [4*cm, 2.5*cm, 2.5*cm, 8*cm]
    else:
        col_widths = None

    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2744")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("WORDWRAP", (0, 0), (-1, -1), True),
    ]))
    story.append(t)
    story.append(Spacer(1, 6))
