"""
Kandidaten-Router: Personalisierter Testzugang für Testgruppen.

Endpunkte:
  GET  /k/{code}          → Prüft Code, startet Test (max. N Nutzungen)
  POST /api/kandidat/abschluss  → Wird nach Testabschluss aufgerufen, sendet E-Mail

Konfiguration (in .env):
  KANDIDAT_EMAIL_TO      = empfaenger@beispiel.de
  KANDIDAT_EMAIL_FROM    = absender@beispiel.de
  KANDIDAT_SMTP_HOST     = smtp.beispiel.de
  KANDIDAT_SMTP_PORT     = 587
  KANDIDAT_SMTP_USER     = user
  KANDIDAT_SMTP_PASS     = passwort
  KANDIDAT_BASE_URL      = https://deine-domain.ch  (für Links im E-Mail)
"""
import smtplib
import os
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.database import (
    GutscheinCode, Hilfssprache, PAKET_MODULE, PaketTyp,
    SessionStatus, TestSession, ZahlungsStatus, get_db,
)
from app.services import session_service

router = APIRouter()

# ── Konfiguration aus .env ────────────────────────────────────────────────────

def _cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ── Kandidaten-Einstieg ───────────────────────────────────────────────────────

@router.get("/k/{code}", response_class=HTMLResponse)
async def kandidaten_einstieg(
    code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Personalisierter Testzugang via QR-Code.
    Prüft den Code, zählt die Nutzung hoch und leitet direkt zum Test weiter.
    """
    code_upper = code.upper().strip()

    # Code in DB suchen
    result = await db.execute(
        select(GutscheinCode).where(GutscheinCode.code == code_upper)
    )
    gc = result.scalar_one_or_none()

    if not gc:
        return HTMLResponse(_fehler_seite("Ungültiger Code", "Dieser Zugangscode existiert nicht."), status_code=404)

    if not gc.aktiv:
        return HTMLResponse(_fehler_seite("Code deaktiviert", "Dieser Zugangscode wurde deaktiviert."), status_code=403)

    if gc.genutzt >= gc.max_nutzungen:
        return HTMLResponse(_fehler_seite(
            "Maximale Nutzungen erreicht",
            f"Dieser Code wurde bereits {gc.genutzt}× verwendet (Maximum: {gc.max_nutzungen})."
        ), status_code=403)

    # Nutzung zählen
    gc.genutzt += 1
    await db.commit()

    # Neue Test-Session erstellen (Premium, Deutsch, Demo-Bypass)
    paket = gc.paket if gc.paket else PaketTyp.premium
    sess = await session_service.erstelle_session(db, paket, Hilfssprache.de, "CHF")
    sess.zahlungs_status = ZahlungsStatus.bezahlt
    sess.status = SessionStatus.laufend

    # Kandidaten-Code in Notiz-Feld der Session speichern (für E-Mail-Zuordnung)
    # Wir nutzen stripe_payment_intent_id als provisorisches Notizfeld
    sess.stripe_payment_intent_id = f"kandidat:{code_upper}:{gc.genutzt}"
    await db.commit()

    return RedirectResponse(url=f"/test/{sess.token}", status_code=302)


# ── E-Mail-Versand nach Testabschluss ────────────────────────────────────────

@router.post("/api/kandidat/abschluss")
async def kandidat_abschluss(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Wird vom Frontend nach Testabschluss aufgerufen.
    Sendet eine E-Mail mit dem Ergebnis-Link an den konfigurierten Empfänger.
    """
    body = await request.json()
    token = body.get("token", "")

    if not token:
        raise HTTPException(status_code=400, detail="Token fehlt.")

    result = await db.execute(
        select(TestSession)
        .where(TestSession.token == token)
        .options(selectinload(TestSession.module))
    )
    sess = result.scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    # Kandidaten-Code aus dem Notiz-Feld extrahieren
    kandidat_info = ""
    if sess.stripe_payment_intent_id and sess.stripe_payment_intent_id.startswith("kandidat:"):
        teile = sess.stripe_payment_intent_id.split(":")
        if len(teile) >= 3:
            kandidat_info = f"{teile[1]} (Durchgang {teile[2]})"

    # E-Mail senden
    erfolg = await _sende_ergebnis_email(sess, kandidat_info)

    return {"ok": True, "email_gesendet": erfolg}


# ── E-Mail-Hilfsfunktionen ────────────────────────────────────────────────────

async def _sende_ergebnis_email(sess: TestSession, kandidat_info: str) -> bool:
    """Sendet eine E-Mail mit dem Ergebnis-Link."""
    smtp_host = _cfg("KANDIDAT_SMTP_HOST")
    smtp_port = int(_cfg("KANDIDAT_SMTP_PORT", "587"))
    smtp_user = _cfg("KANDIDAT_SMTP_USER")
    smtp_pass = _cfg("KANDIDAT_SMTP_PASS")
    email_to = _cfg("KANDIDAT_EMAIL_TO")
    email_from = _cfg("KANDIDAT_EMAIL_FROM", smtp_user)
    base_url = _cfg("KANDIDAT_BASE_URL", "https://deine-domain.ch")

    if not all([smtp_host, smtp_user, smtp_pass, email_to]):
        print(f"[Kandidat] E-Mail-Konfiguration unvollständig – kein Versand.")
        return False

    # Ergebnis-Daten zusammenstellen
    niveau = sess.gesamt_niveau.value if sess.gesamt_niveau else "–"
    score = f"{sess.gesamt_score:.1f}%" if sess.gesamt_score else "–"
    abgeschlossen = sess.abgeschlossen_am.strftime("%d.%m.%Y %H:%M") if sess.abgeschlossen_am else "–"
    ergebnis_url = f"{base_url}/ergebnis/{sess.token}"
    pdf_url = f"{base_url}/api/export/{sess.token}/pdf"

    # Modul-Tabelle
    modul_zeilen = ""
    for m in sorted(sess.module, key=lambda x: x.reihenfolge):
        modul_name = {
            "m1_grammatik": "M1 Grammatik & Wortschatz",
            "m2_lesen": "M2 Lesen & Leseverstehen",
            "m3_hoerverstehen": "M3 Hörverstehen",
            "m4_vorlesen": "M4 Vorlesen",
            "m5_sprechen": "M5 Freies Sprechen",
            "m6_schreiben": "M6 Schreiben",
        }.get(m.modul.value, m.modul.value)
        modul_cefr = m.cefr_niveau.value if m.cefr_niveau else "–"
        modul_score = f"{m.gesamt_score:.1f}" if m.gesamt_score else "–"
        modul_zeilen += f"<tr><td>{modul_name}</td><td>{modul_cefr}</td><td>{modul_score}</td></tr>"

    html = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333; max-width: 600px;">
    <h2 style="color: #2563eb;">🎓 DaF Sprachdiagnostik – Neues Testergebnis</h2>

    <table style="border-collapse: collapse; width: 100%; margin-bottom: 1rem;">
      <tr><td style="padding: 6px; font-weight: bold;">Kandidat:</td><td style="padding: 6px;">{kandidat_info or "–"}</td></tr>
      <tr><td style="padding: 6px; font-weight: bold;">Abgeschlossen:</td><td style="padding: 6px;">{abgeschlossen}</td></tr>
      <tr><td style="padding: 6px; font-weight: bold;">CEFR-Niveau:</td><td style="padding: 6px; font-size: 1.2em; font-weight: bold; color: #2563eb;">{niveau}</td></tr>
      <tr><td style="padding: 6px; font-weight: bold;">Gesamtscore:</td><td style="padding: 6px;">{score}</td></tr>
      <tr><td style="padding: 6px; font-weight: bold;">Session-Token:</td><td style="padding: 6px; font-family: monospace; font-size: 0.85em;">{sess.token[:16]}…</td></tr>
    </table>

    <h3>Modul-Ergebnisse</h3>
    <table style="border-collapse: collapse; width: 100%;">
      <tr style="background: #f1f5f9;">
        <th style="padding: 8px; text-align: left; border: 1px solid #e2e8f0;">Modul</th>
        <th style="padding: 8px; text-align: left; border: 1px solid #e2e8f0;">CEFR</th>
        <th style="padding: 8px; text-align: left; border: 1px solid #e2e8f0;">Score</th>
      </tr>
      {modul_zeilen}
    </table>

    <div style="margin-top: 1.5rem;">
      <a href="{ergebnis_url}" style="background: #2563eb; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px; margin-right: 10px;">
        📊 Ergebnis ansehen
      </a>
      <a href="{pdf_url}" style="background: #64748b; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px;">
        📄 PDF herunterladen
      </a>
    </div>

    <p style="margin-top: 2rem; font-size: 0.8em; color: #94a3b8;">
      Diese E-Mail wurde automatisch von der DaF Sprachdiagnostik-Plattform generiert.
    </p>
    </body></html>
    """

    text = f"""DaF Sprachdiagnostik – Neues Testergebnis

Kandidat: {kandidat_info or "–"}
Abgeschlossen: {abgeschlossen}
CEFR-Niveau: {niveau}
Gesamtscore: {score}

Ergebnis: {ergebnis_url}
PDF: {pdf_url}
"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"DaF Test abgeschlossen – {kandidat_info or sess.token[:8]} – Niveau {niveau}"
        msg["From"] = email_from
        msg["To"] = email_to
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(email_from, [email_to], msg.as_string())

        print(f"[Kandidat] E-Mail gesendet für {kandidat_info} → {email_to}")
        return True

    except Exception as e:
        print(f"[Kandidat] E-Mail-Fehler: {e}")
        return False


# ── Fehler-Seite ──────────────────────────────────────────────────────────────

def _fehler_seite(titel: str, nachricht: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{titel} – DaF Sprachdiagnostik</title>
  <link rel="stylesheet" href="/static/css/main.css">
</head>
<body>
<nav class="navbar">
  <div class="navbar-inner">
    <a href="/" class="navbar-brand"><div class="brand-icon">🎓</div>DaF Sprachdiagnostik</a>
  </div>
</nav>
<div class="container-sm" style="padding-top:4rem; text-align:center;">
  <div style="font-size:3rem; margin-bottom:1rem;">🚫</div>
  <h1 style="color:#ef4444;">{titel}</h1>
  <p class="text-muted mt-3">{nachricht}</p>
  <a href="/" class="btn btn-outline mt-6">← Zur Startseite</a>
</div>
</body>
</html>"""
